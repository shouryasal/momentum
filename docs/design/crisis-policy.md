# Crisis policy — what Earn does when the world changes

Status: design, measured. Written 2026-09-24 from three studies against real Binance candles
(1.06M+ bars, BTC/ETH from 2017-08-17), the live runtime copy at `~/earn-run`, and a read of the
shipped code. Costs on throughout: 10 bps per side for BTC/core, 30 bps per side for the alt book
(the measured $5–20M-ADV round trip, `wide-universe.md` §1.3). Every rule tested fires on
information available at the time; every execution is at the next bar.

Provenance: Study 1 (22 named shocks 2020–2026, hold/cut/dip policy comparison, full-sample
walk 2019-01-01 → 2026-09-23), Study 2 (38 drawdown episodes discovered from candles, 26 since
2019; tripwire search, response tiers, re-entry), Study 3 (code-and-latency audit of the shipped
system and the live runtime). Where two studies disagree, both numbers are given and the
disagreement is explained rather than averaged.

---

## 0. The answer first, because it is a negative

**Doing nothing and holding beat every crash-reactive intervention that was tested, and the
intervention the system already ships is the most expensive one of them all.**

Measured continuously over 7.73 years (2019-01-01 → 2026-09-23), 8 equal-weight spot names chosen
at each point by trailing-30d dollar volume, gross 0.80 / cash 0.20, costs on:

| policy | CAGR% | Sharpe | MaxDD% | exits/yr | fee %/yr | terminal × |
|---|---|---|---|---|---|---|
| **hold, never react** | **30.53** | 0.41 | **−86.70** | 0.0 | 0.00 | **7.84** |
| cut when BTC 24h ≤ −8% | 6.74 | 0.11 | **−92.76** | 22.0 | 3.52 | 1.66 |
| cut when BTC 24h ≤ −12% | 18.40 | 0.28 | −87.94 | 6.9 | 1.10 | 3.69 |
| **the shipped `risk.daily_loss_stop` shape: −3% NAV → flatten + 24h lock** | **−5.23** | **−0.10** | **−94.55** | **66.7** | **10.66** | **0.66** |
| same trigger, HALVE instead of flatten | 13.05 | 0.21 | −89.22 | 66.7 | 5.33 | 2.58 |
| same shape at −5% NAV | 4.14 | 0.07 | −92.99 | 25.4 | 4.06 | 1.37 |
| same shape at −8% NAV | 13.32 | 0.20 | −88.58 | 6.9 | 1.10 | 2.63 |
| trend filter (close > MA125d) | **35.68** | **0.65** | **−74.86** | 25.5 | 4.08 | **10.56** |
| trend filter + crash cut at −8%/24h | 20.50 | 0.43 | −75.77 | 33.4 | 5.34 | 4.22 |
| trend filter + crash cut at −12%/24h | 28.77 | 0.58 | −70.03 | 28.1 | 4.49 | 7.06 |
| buy the dip as literally specified | 33.78 | 0.50 | −79.96 | 4.8 | 0.08 | — |

Four things in that table, in order of how much they should change the build plan.

**1. Every crash-reactive cut destroyed value and made drawdown worse, not better.** The −8%/24h
cut turns −86.70% max drawdown into **−92.76%**. This is not a bug: that policy's drawdown peak is
2021-05-10 and its trough is 2026-06-25 — the equity curve never regains the 2021 peak. The deeper
drawdown is continuous whipsaw bleed, not a worse crash.

**2. The shipped daily stop, in its shipped shape, is the worst policy measured.** On this book
`risk.daily_loss_stop: 0.03` with a flatten fires **66.7 times a year**, spends **10.66%/yr in
fees — 89% of the entire `risk.max_fee_pct_per_month` budget (12%/yr) on stop-outs alone** — turns
+30.5% CAGR into −5.2% (0.66× terminal: it loses a third of capital over 7.7 years), and ends with
a **worse** max drawdown than doing nothing. Study 2 reached the same verdict independently by a
different route: modelled as a −3.75% move in the risk asset at 0.80 gross with a 24h lock, it
scores ΔCAGR **−23.82** / ΔMaxDD **−1.63** against buy-and-hold.

**And the flatten is what does the damage, not the trigger.** Halving instead of flattening at the
identical −3% trigger goes from −5.23% to **+13.05%** CAGR.

**3. A crash trigger adds nothing on top of the trend filter Earn already owns.** MA125d alone:
35.68 / 0.65 / −74.86. Add the −8%/24h cut: **20.50 / 0.43 / −75.77** — fifteen points of CAGR and
0.22 of Sharpe surrendered for one point of drawdown. This is the comparison that matters, because
it is incremental to what is already shipped, and it is the comparison Study 2 never ran: Study 2's
encouraging −25% tier (ΔCAGR +1.43 / ΔMaxDD +5.95) is measured against **buy-and-hold**, which is
not the system Earn runs. Do not read Study 2's +1.43 as +1.43 for Earn.

**4. Why the trend filter survives a crisis is not what it looks like.** It was **already out of
the market at T0 in 11 of 22 events** — 23 days out before the 2022-01-21 correction, 131 days
before Terra, 314 days before FTX, 100 days before 2026-02-05. It does not react to crises; it is
absent from the regimes that produce them. That is a structural property, and it is why it survives
the full-sample test while every reactive rule fails it.

So the policy is: **hold, stop buying, and never sell into a crash on a numeric trigger.** The one
free tier is blocking new entries. Everything else has to clear a bar that nothing has cleared yet.

---

## 1. What actually happens in a crash

### 1.1 Shape and speed — the first hour is nearly empty

BTC, % from T0 close, where T0 = 00:00 UTC of the conventionally dated day.

| event | class | +1h | +4h | +12h | +24h | +7d | h to 7d low | 7d trough |
|---|---|---|---|---|---|---|---|---|
| covid wave 1 (equities) 2020-02-24 | telegraphed | −0.61 | −1.90 | −1.52 | −3.31 | −13.96 | 163 | −15.43 |
| covid oil circuit breaker 2020-03-09 | instant | +0.04 | −2.85 | −2.48 | −1.78 | −34.98 | 98 | −53.20 |
| covid Black Thursday 2020-03-12 | instant | −2.53 | −3.65 | −23.82 | **−41.18** | −32.43 | 26 | **−52.21** |
| Apr 2021 leverage flush | instant | −0.28 | −7.67 | −7.48 | −4.40 | −16.11 | 128 | −20.02 |
| Tesla/BTC halt 2021-05-12 | instant | −0.55 | +0.01 | −1.80 | −13.40 | −25.70 | 137 | −26.76 |
| May 2021 China ban 2021-05-19 | instant | **−4.15** | −7.75 | −17.31 | −15.94 | −10.59 | 13 | −29.59 |
| El Salvador flash 2021-09-07 | instant | −0.02 | +0.04 | −2.89 | −10.41 | −13.96 | 15 | −18.39 |
| Dec 2021 omicron flush | instant | −0.11 | −5.25 | −11.29 | −7.73 | −9.98 | 5 | −20.82 |
| Jan 2022 Nasdaq correction | telegraphed | −2.77 | −5.02 | −6.62 | −10.78 | −9.14 | 85 | −19.51 |
| **Ukraine invasion 2022-02-24** | instant | +0.53 | −4.57 | −4.08 | **+4.00** | **+19.65** | 5 | −6.62 |
| Terra/LUNA 2022-05-09 | telegraphed | −0.26 | −1.45 | −3.76 | −9.79 | −8.88 | 77 | −21.80 |
| Celsius/3AC 2022-06-13 | telegraphed | −2.04 | −3.49 | −9.97 | −17.29 | −23.75 | 140 | −33.01 |
| FTX collapse 2022-11-08 | instant | +0.56 | −4.28 | −3.86 | −10.62 | −19.25 | 47 | −24.01 |
| **SVB / USDC depeg 2023-03-09** | instant | −0.08 | +0.10 | −0.50 | −7.36 | **+11.43** | 34 | −9.99 |
| SEC sues Binance 2023-06-05 | instant | +0.08 | −0.69 | −1.24 | −4.94 | −3.92 | 36 | −6.19 |
| Aug 2023 deleveraging | instant | +0.18 | +0.24 | −0.30 | −6.45 | −7.23 | 21 | −11.95 |
| Iran/Israel weekend 2024-04-13 | instant | −1.53 | +1.01 | +0.90 | −4.84 | −5.18 | 146 | −10.93 |
| yen carry unwind 2024-08-04 23:00 | instant | −3.42 | −7.34 | −11.73 | −7.12 | **+0.95** | 7 | −15.75 |
| Bybit hack 2025-02-21 | instant | +0.08 | −0.03 | +0.68 | −2.09 | −14.23 | 140 | −16.27 |
| **tariff shock 2025-04-06** | instant | −0.12 | −0.03 | −0.99 | −6.22 | **+2.36** | 31 | −10.80 |
| Oct 2025 liquidation 2025-10-10 | instant | −0.10 | −0.42 | −0.12 | −7.58 | −10.63 | 21 | −16.22 |
| Feb 2026 flush (data-flagged) | instant | −0.61 | −2.72 | −4.53 | −12.84 | −7.42 | 24 | −17.66 |

**In 18 of 22 events |+1h| < 2.6%.** Mean +1h across all 22 = **−0.81%**; mean +24h = **−9.19%**.
The information arrives over 4–24 hours, not in the first bar. That single fact is why "we need a
faster feed" is the wrong answer to this question.

The first hour is not empty of *hazard*, though. Unconditionally P(next 24h ≤ −8%) = **1.9%**.
Conditional on one hour ≤ −2% / −3% / −5%: **12.6% / 17.2% / 21.1%** (n = 1092 / 418 / 90). A 9–11×
lift on the tail, with median forward 24h only −2.2% to −4.2%. Hazard, not direction — the same
shape `crypto-research.md` §1.2 found for funding.

**Four events where crypto rose while the world fell.** Ukraine invasion: BTC **+4.0% at 24h,
+19.7% at 7d**. SVB/USDC depeg: −7.4% at 24h but **+11.4% at 7d, +28.4% at 30d** — USDT was the
refuge, which is exactly the `usdt_premium` state `venue-guard` already names. Tariff shock +2.4% at
7d, +13.3% at 30d. Yen carry +0.95% at 7d. Selling any of these on a numeric trigger would have been
the single worst action available on the day.

### 1.2 Volume — loud, and useless for timing

| event | peak × normal hourly | first 24h × normal | peak hour minus 7d-low hour | hours >2× in 7d | hours elevated after the low |
|---|---|---|---|---|---|
| covid Black Thursday | 20.7× | **6.03×** | 0 | 127 | 385 |
| covid oil circuit breaker | 22.3× | 2.67× | 0 | 106 | 386 |
| May 2021 China ban | 15.9× | 5.56× | 0 | 99 | 170 |
| Terra/LUNA | 21.4× | 6.52× | **−16** | 115 | 78 |
| Celsius/3AC | 13.7× | 5.76× | **−74** | 118 | 159 |
| FTX collapse | 9.1× | 3.57× | **−29** | 66 | 1 |
| yen carry unwind | 24.1× | **7.84×** | 0 | 66 | 113 |
| **Oct 2025 liquidation** | **50.9×** | 6.27× | 0 | 94 | 92 |
| Feb 2026 flush | 31.1× | **9.93×** | 0 | 103 | 210 |
| mean, all 22 | **17.7×** | **4.08×** | — | 71 | 143 |

**Volume does not mark the bottom.** Peak volume landed within ±6h of the 7-day low in only **10 of
22** events; it peaked *before* the low in 11 and after in 2; median |peak − low| = **12 hours**.
Volume stayed above 1.5× normal for a median **~100 hours after** the low. "Capitulation volume =
buy" is not a timing rule at 1h resolution; it is a description of the bar you are already inside.

### 1.3 Order flow — one dead end, two usable facts

**Dead end: there are no gaps.** Across all 22 events × 72h the largest single hour-to-hour
open-vs-prior-close gap was **0.190%**, and the count of gaps > 0.5% was **zero in every event**
(calm-period max 0.118%). Crypto is continuous. Gap-fill and gap-open logic has nothing to act on
and must never be written.

**Usable: the down/up volume split flips at the low.** Reconstructed from intrabar close position,
down/up volume runs **1.19** into the low and **0.81** in the 24 hours after it, against a calm
baseline of 0.99. Present in 20 of 22 events. It is a *confirmation* variable — available only
after the low — so it can never anticipate.

**Usable: range expansion is the loudest thing in the tape.** **15.3×** the trailing-30d median
hourly range at the peak, **2.2×** sustained over 72h, with no threshold tuning needed. It is a
volatility-regime input, not an entry or exit.

### 1.4 Breadth and correlation — the alt problem

| event | n pairs | % alts down 24h | median alt 24h | BTC 24h | **median alt − BTC** | corr calm 30d | corr event 72h |
|---|---|---|---|---|---|---|---|
| **May 2021 China ban** | 35 | 100.0 | −40.12 | −15.94 | **−24.18** | 0.612 | 0.860 |
| **Oct 2025 liquidation** | 89 | 98.9 | −25.92 | −7.58 | **−18.34** | 0.538 | 0.746 |
| Iran/Israel weekend | 64 | 95.2 | −13.69 | −4.84 | **−8.85** | 0.638 | 0.871 |
| Terra/LUNA | 41 | 100.0 | −16.72 | −9.79 | −6.93 | 0.631 | 0.856 |
| El Salvador flash | 39 | 92.1 | −16.47 | −10.41 | −6.06 | 0.518 | 0.754 |
| tariff shock | 79 | 100.0 | −12.11 | −6.22 | −5.89 | 0.644 | 0.795 |
| FTX collapse | 44 | 100.0 | −15.88 | −10.62 | −5.26 | 0.496 | 0.801 |
| Feb 2026 flush | 104 | 100.0 | −16.46 | −12.84 | −3.62 | 0.551 | 0.747 |
| covid Black Thursday | 9 | 100.0 | −40.96 | −41.18 | +0.22 | 0.722 | 0.914 |
| Celsius/3AC | 42 | 95.1 | −8.88 | −17.29 | **+8.41** | 0.647 | 0.790 |
| **mean, all 22** | — | **91.8** | — | — | **−3.75** | **0.58** | **0.78** |
| mean, INSTANT (n=18) | — | 90.2 | — | — | **−4.40** | 0.57 | 0.78 |
| mean, TELEGRAPHED (n=4) | — | 98.8 | — | — | −0.81 | 0.62 | 0.81 |

This confirms and sharpens `wide-universe.md` §2.1. Mean pairwise 1h correlation rises from **0.58
calm to 0.78** in the 72h event window, in **21 of 22 events**. The one exception is the Bybit hack
(0.729 → 0.603) — an idiosyncratic single-venue event, which is *why* it is the exception, and which
is the one genuinely useful classification news can supply.

**Dispersion takes weeks to come back, not days.** Hours from T0 until 72h-rolling mean pairwise
correlation falls back to its own calm-30d level: median **~350 hours (15 days)**; 6 of 22 never
returned inside 30 days. Fastest were the tariff shock (162h) and Ukraine (186h).

### 1.5 Recovery — where the permanent loss lives

Hours from each coin's own trough to regain 25 / 50 / 100% of its T0→trough drop, capped at 365 days.

| event | core 25% | core 50% | core 100% | alt 25% | alt 50% | alt 100% | % alts no full recovery in 365d | **% alts still below T0 today** |
|---|---|---|---|---|---|---|---|---|
| covid Black Thursday | 1h | 154h | 1035h | 0h | 8h | 1084h | 0.0 | **0.0** |
| covid oil circuit breaker | 1h | 158h | 1136h | 0h | 39h | 1111h | 0.0 | **0.0** |
| Apr 2021 leverage flush | 0h | 0h | 63h | 1h | 21h | 171h | 15.6 | 87.5 |
| Tesla/BTC halt | 1h | 1916h | 3594h | 0h | 23h | 2255h | **59.4** | 90.6 |
| May 2021 China ban | 0h | 2h | 2309h | 0h | 3h | 2585h | **40.6** | 87.5 |
| Terra/LUNA | 5h | 20h | 4246h | 9h | 69h | 2885h | **65.8** | 71.1 |
| FTX collapse | 31h | 37h | 1569h | 16h | 317h | 1631h | 17.1 | 61.0 |
| SVB / USDC depeg | 3h | 15h | 61h | 2h | 12h | 58h | 0.0 | 64.3 |
| yen carry unwind | 0h | 8h | 81h | 1h | 8h | 43h | 0.0 | 71.2 |
| Oct 2025 liquidation | 18h | 0h | 41h | 0h | 0h | 721h | **59.3** | 89.5 |
| **mean, all 22** | — | — | — | — | — | — | **17.1** | **65.1** |

Blunt pattern. **The first 25% of the drop comes back in hours** — median 1–18h for core, 0–18h for
alts — which is precisely why cutting near the bottom is so expensive. But **65% of the alts in
these events are still below their T0 price today**, 87–91% in the 2021 cohort. Core (BTC/ETH/BNB)
fully recovered inside 365 days in **22 of 22** events.

**BTC/ETH drawdown is rent. Satellite drawdown is a purchase.** That asymmetry, not the size of the
BTC move, is what any crisis action should be aimed at.

### 1.6 Three species of event, and only one is tradeable in advance

| metric | INSTANT (n=18) | TELEGRAPHED (n=4) |
|---|---|---|
| BTC +1h | −0.67% | −1.42% |
| BTC +24h | −8.94% | −10.29% |
| BTC +7d | −9.85% | −13.93% |
| hours to 30d trough | 233 | 184 |
| peak volume × normal | 18.4× | 14.1× |
| peak 1h range × normal | 16.3× | 11.0× |
| % alts down 24h | 90.2 | 98.8 |
| **median alt − BTC** | **−4.40pp** | −0.81pp |
| % alts no full recovery 365d | 15.7 | 23.7 |

Telegraphed events (Jan 2022 rates, Terra, Celsius/3AC, covid wave 1) are **deeper and slower** and
hit breadth harder, but they do **not** punish alts relative to BTC — everything falls together.
Instant events are shallower on BTC and **5× worse for the alt-vs-BTC gap**: the cascade is a
perp-liquidation event priced in the thin books first.

SCHEDULED is the third species and the only one where advance action is possible. From
`crypto-research.md` §1.7 and the `event-blackout` skill body: 79 FOMC releases give median
|T→T+4h| **0.850% vs 0.496%** unconditional (**1.72×**), P(|4h| > 2%) 20.3% vs 10.9%, direction
t = −1.85 at +1h and **−0.49 at +24h**. Elevation runs **T−7h to T+8h**, peaking at T−1h (2.26×).
`risk.blackout.window_minutes` is symmetric at 60 and therefore covers only the peak hour. The data
supports roughly **−360 / +480 minutes**. Double the variance for no expected return is a pure cost
to enter into, and the only correct action is to not trade.

---

## 2. What the system should do

### Tier 0 — HOLD. This is the default and it is the measured winner.

Do not sell into a crash on a numeric trigger. Section 0 has the accounting. The specific reason,
in one row: the shipped `signals.scanner.detectors.move` thresholds, measured on BTC over the full
sample, all predict forward **return upward** while predicting the tail.

| rule | n bars | fwd 168h max dd | base | P(fwd 168h dd ≤ −15%) | base | **fwd 168h RETURN** | base |
|---|---|---|---|---|---|---|---|
| 1h ≤ −2.5% | 327 | −7.96% | −5.00% | 17.4% | 5.5% | **+2.59%** | +1.12% |
| 4h ≤ −5.0% | 296 | −8.13% | −5.00% | 15.9% | 5.5% | **+3.54%** | +1.12% |
| 24h ≤ −8.0% | 892 | −9.07% | −5.00% | 17.6% | 5.5% | **+2.47%** | +1.12% |
| 24h ≤ −12.0% | 200 | −10.62% | −5.00% | 25.5% | 5.5% | **+3.90%** | +1.12% |

Every threshold predicts a −15% week at 3.2–4.6× the base rate **and** predicts forward return at
+2.5% to +3.9% against a +1.12% base. **Selling on these triggers sells into a positive expected
return.** Study 2 measured the same thing from the other side: forward 96h median return after its
best tripwire fires is **+1.06% against +0.29% unconditional**, while the p10 drawdown worsens from
−8.95% to −12.36%. The tail gets worse and the centre gets better.

The only place direction turns negative is when the trend is already broken:

| BTC 24h ≤ −3σ | n | +96h median | +96h mean | +168h median |
|---|---|---|---|---|
| above the 200d MA | 32 | +0.70 | +1.57 | +1.01 |
| **below the 200d MA** | 33 | **−1.88** | **−1.63** | −0.72 |

Which is the trend filter's result again, arriving by a third route.

### Tier 1 — STOP BUYING. Free, and the one tier the evidence unambiguously supports.

Block new entries for a bounded window. It touches no held position, costs zero in fees, and
forgoes only entries.

Measured, with a 48h flag window covering 10.4% of all hours:

| horizon | in-flag median | in-flag mean | in-flag sd | out-of-flag median | out mean | out sd |
|---|---|---|---|---|---|---|
| 24h | +0.22 | +0.27 | **3.98** | +0.08 | +0.15 | **3.07** |
| 168h | +0.74 | +0.57 | **9.54** | +0.46 | +1.19 | **8.38** |
| 336h | +0.30 | **−0.01** | 11.76 | +1.12 | **+2.55** | 12.53 |

Entering inside a flag window buys **14–30% more variance for the same or lower expected return**,
and at two weeks it is the difference between a **−0.01%** and a **+2.55%** mean. This is the
`event-blackout` argument with a numeric trigger instead of a calendar.

**Cost when the drop turns out to be a V:** the forgone 48h of entries. On the four events that
rose (Ukraine +19.65% at 7d, SVB +11.43%, tariff +2.36%, yen carry +0.95%) a 48h entry block costs
the entries not made, not the positions not held — the book keeps everything it had. That is the
whole reason this tier is cheap and every selling tier is not.

Severity: `block_entries`, scope ALL, bounded expiry. It must never be able to cause an exit, and it
must never be able to *stop* one (`check_exit` already returns allowed unconditionally, including
under KILL — that is correct and must stay).

### Tier 2 — TIGHTEN THE SATELLITE SLEEVE. Directionally right, not yet earned.

If anything is to be reduced, it is `risk.max_satellite_gross` and `risk.max_satellite_positions`,
before anything touches core. The justification is §1.5: median alt − BTC is −3.75pp overall and
−4.40pp for INSTANT events, worst −24.18pp (May 2021: median alt −40.12% vs BTC −15.94%) and
−18.34pp (Oct 2025: −25.92% vs −7.58%); core fully recovered in 22 of 22 events while 17.1% of alts
on average never did, and 65.1% are still below T0 today.

**But this tier does not ship yet, and the honest reason is arithmetic.** Study 2's −25% tier is the
only response positive on both metrics in every sub-period it tested (BTC ΔCAGR +1.43 / ΔMaxDD
+5.95, thirds +7.95/+5.95/+4.50; the 8-name book +0.60 / +7.08, thirds +9.47/+7.08/+6.16; fee drag
0.65–1.94%/yr). Three problems:

1. **The baseline is wrong for Earn.** Those deltas are against buy-and-hold. Study 1's incremental
   test — reactive cut *on top of* the trend filter — cost 15 points of CAGR for 1 point of
   drawdown. Nobody has run a −25% partial tier incremental to the trend filter. **That is the
   single measurement that would justify this tier, and it does not exist yet.**
2. **ΔCAGR flips sign between halves in every configuration tested.** In 2019-01 → 2021-06 the −25%
   tier cost the alt book **−34.17pp of CAGR** and BTC −3.79pp. Drawdown improved in all three
   thirds; return did not. The correct claim is narrow: *this is drawdown insurance whose premium is
   sometimes zero and sometimes a third of the return, and nothing measured tells you which in
   advance.*
3. **It is not executable at live seed size.** At the `trading.max_seed_usdt` ceiling of 1000 a
   satellite position is `risk.tier_caps.satellite` = 5% = **$50**, and a −25% trim is a **$12.50**
   order, below `risk.min_notional_usdt` = 25. So the recommended response is physically
   unexecutable on exactly the names it most needs to apply to. Core and major are fine ($100 /
   $75 / $37.50); −50% is exactly $25, at the boundary.

Verdict: build the *mechanism* (§6, item 4), because it does not exist and tier 1 needs part of it
anyway; do not arm the selling part until the incremental test is run.

### Tier 3 — FLATTEN. Already shipped, and it is the expensive mistake.

`risk.daily_loss_stop: 0.03` → `flatten` + `risk.daily_stop_lock_hours: 24`. Section 0 has the
numbers twice over. Two independent fixes, both measured:

* **widen the trigger**: −5% → +4.14% CAGR; −8% → +13.32% CAGR at 6.9 exits/yr and 1.10% fee/yr; or
* **keep −3% and halve instead of flatten**: +13.05% CAGR at the same trigger.

Either is a change proposal on a tier-2 path (`config/**`), human-only, with the walk-forward
attached. Do not touch it from a run, and **do not let a good paper quarter be read as validation**
— `crypto-research.md` §3 puts the power requirement for distinguishing these at 219 years.

A separate execution note: the same simulation understates the real damage. 66.7 flatten-and-rebuild
cycles a year at 4–8 positions each would breach `risk.max_orders_per_day: 16` and
`risk.max_turnover_pct_per_day: 0.50` in practice, so the gate would block the *rebuild* rather than
execute it cleanly. The shipped behaviour is worse than the backtest, not better.

### What "buy the dip" actually is

As literally specified — deploy the `risk.usdt_floor` cash in tranches at −15 / −25 / −35% drawdown
— it is the **worst policy at every horizon**: 30d drawdown 4.6pp deeper than hold, 90d 6.6pp, 365d
median 10pp, with no return gain, because it spends the reserve on something that keeps falling.

The variant that beat hold (33.78% CAGR / −79.96% MaxDD, 4.8 trades/yr, 0.08% fee/yr) rearms only
after a new 30d high and trims back to 0.80 gross to rebuild the reserve. **That is a rebalance
rule, not a dip-buying rule**, and its value is rebalancing. It belongs in `execution.rebalance_band`
and `risk.usdt_floor` through `strategy-lab`, not in a crisis module.

---

## 3. How it knows

### 3.1 The tripwire. One rule, two conditions.

```
z24  = BTC 24h log return / (7d stdev of 1h returns × sqrt(24))     # sigma window shifted one bar
bdn8 = share of eligible pairs (>= 30d history) whose 24h return <= -8%

FIRE when   z24 <= -2.0   AND   bdn8 >= 0.30
```

Measured 2019-01-01 → 2026-09-23, 67,685 hours, 48h fire cooldown. Base rate
P(forward 96h drawdown ≤ −8%) = **12.0%**.

| tripwire | fires/yr | P(dd96 ≤ −8%) | false alarms/yr | episodes caught | median lead to bottom |
|---|---|---|---|---|---|
| **z24 ≤ −2 AND bdn8 ≥ 0.30** | **18.9** | **0.240** | **14.4** | **25/26** | **30h** |
| z24 ≤ −3 | 7.8 | 0.283 | 5.6 | 22/26 | 16h |
| bdn8 ≥ 0.70 | 17.3 | 0.231 | 13.3 | 25/26 | 15h |
| median alt 24h − BTC 24h ≤ −8% | 8.0 | 0.242 | 6.1 | 14/26 | 32h |
| z4 ≤ −2.5 AND bdn8 ≥ 0.30 | 18.2 | 0.234 | 14.0 | 25/26 | 27h |
| z4 ≤ −2.5 alone | 46.9 | 0.163 | 39.2 | 26/26 | 36h |
| Amihud z ≥ 1.5 AND z24 ≤ −1.5 | 5.7 | 0.295 | 4.0 | 11/26 | 28h |
| **range expansion ≥ 3× ATR** | 76.1 | **0.124** | 66.7 | 25/26 | 39h |
| **volume z ≥ 2** | 93.8 | **0.137** | 81.0 | 26/26 | 42h |
| **universe-wide volume z ≥ 1.5** | 88.0 | **0.124** | 77.1 | 26/26 | 44h |
| **correlation spike ≥ +0.15 over 30d median** | 36.1 | **0.133** | 31.3 | 18/26 | 32h |
| **funding flips negative** | 8.8 | 0.147 | 7.5 | **3/26** | 42h |
| **funding ≥ 90th pct of 1y** | 19.9 | **0.104** | 17.9 | 5/26 | 12h |
| **Amihud illiquidity z ≥ 1.5** | 52.0 | **0.117** | 45.9 | 18/26 | 38h |
| **Corwin–Schultz spread z ≥ 3** | 65.8 | **0.145** | 56.3 | 25/26 | 47h |

Read the bold rows first: they are the ones that were expected to work and do not. **Volume
z-score, range expansion, correlation spike, funding level, funding flip, and both candle-derivable
liquidity proxies have essentially no lift over the 12.0% base.** Volume and range expansion fire in
26 of 26 episodes — and 76–94 times a year, which is what "coincident with a crash" means
numerically. Funding at the 90th percentile sits *below* the base rate at 0.104, exactly consistent
with `crypto-research.md` §1.2: funding predicts drawdown at a *weekly* horizon and is useless as an
hourly trigger.

Only two families lift: a **vol-normalised return shock** and **cross-sectional breadth**. Together
they reach 0.240 — a 2.0× lift, which means **76% of alarms are still false**, at 14.4 false alarms
a year, one every 19 days.

Study 1 found the same family independently with a stricter breadth form —
`≥90% of the point-in-time book down 24h AND median 24h ≤ −8%`: 22.9 episodes/yr, P(forward 168h
dd ≤ −15%) **11.5% vs 5.5% base** (2.1×), forward 168h return **+1.27% vs +1.12%** (no directional
content), split-half ratio **1.77 (2019-01→2022-11) → 1.32 (2022-11→2026-09)** as absolute levels
compress. Two independent searches, the same two ingredients, the same ~2× lift, the same absence of
direction.

**Express it as a rolling percentile, never a fixed threshold.** Both halves show the ratio holding
while the level compresses — the same pattern funding shows, for the same reason (the market is
calming as the basis trade institutionalises). A fixed −8% breadth threshold measured in 2020 will
fire less and less often through 2028 for reasons that have nothing to do with hazard.

**Do not add the also-rans for completeness.** Each costs false alarms and buys nothing.

### 3.2 False-positive bill, per trigger, deduped into episodes ≥48h apart

Precision = share of episodes falling inside any event window (T0−24h .. T0+7d).

| trigger | episodes | per year | in an event | precision |
|---|---|---|---|---|
| `close < MA200d` | 23 | **2.5** | 7 | **30.4%** |
| `btc_r24h ≤ −12%` | 52 | 5.7 | 15 | 28.8% |
| `btc_r24h ≤ −8%` | 110 | 12.1 | 20 | 18.2% |
| `btc_r6h ≤ −5%` | 160 | 17.6 | 22 | 13.8% |
| `btc_r1h ≤ −3%` | 171 | 18.8 | 22 | 12.9% |
| breadth ≥90% & med ≤ −8% | 177 | 22.9 | 25 | 14.1% |
| `vol > 5× & r1h < −1%` | 270 | 29.7 | 30 | 11.1% |
| **`gulfday_dd ≤ −3.75%` (the shipped daily stop)** | **276** | **30.3** | 25 | **9.1%** |

The shipped daily stop has the **worst precision of anything measured** and the highest firing rate.
That is the same finding as §0 seen through a different lens.

**Write the false-alarm rate into the operator surface and into `lessons.md` before the first
firing.** The system will raise ~19 alarms a year of which ~14 are followed by no 8% drawdown. If
that is not stated up front, the first three false alarms will produce pressure to loosen the
threshold — and the 24-cell sensitivity grid says every nearby threshold has a **worse** return
profile with no better drawdown profile. The post-mortem must grade a firing on **process** (did the
rule fire on information available at the time; did the response execute inside its latency budget)
and **never** on whether a crash followed. Grading it on outcome destroys it within a quarter.

### 3.3 Latency, in real units

Every lag below is read from shipped config, not estimated.

| path | config | real lag |
|---|---|---|
| exchange-side per-trade stop (LIVE, market order) | `trading.defaults.stoploss.fixed_pct`, `on_exchange: auto` (true in LIVE) | **seconds**, and fires with the bot down |
| daily/monthly NAV stop → flatten + lock | `riskgate.loop_tick` via `earn_base.bot_loop_start`, `process_throttle_secs: 5` | **~5 s** after the mark moves |
| a flag already written → honoured | `flags_blocked` re-reads `knowledge/flags.json` uncached on every `check_entry` | **~0 s**, bounded by the next entry attempt (candle close) |
| candle data the gate and scanner read | `ops.schedules.ingest` cron `*/15`, `risk.staleness_minutes: 30` | **0–15 min** stale; entries blocked past 30 min |
| scanner detector → candidate | `signals.scanner.cron: */5`, `run_after_ingest: true` | **0–5 min** after a closed bar → **5–25 min** after the hour |
| local holdings watcher (may only write `watch_events`) | `watch.cron: */7`, `deadline_s: 120`, local Ollama | **0–7 min + ≤2 min**, and it cannot trade |
| screen + validate (LLM) | `validator.deadline_s: 600`, `cooldown_min_per_asset: 120`, `max_per_day: 6` | **1–10 min**, capped 6/day |
| planner → research run → proposal | `planner.max_per_day: 3`, `cooldown_hours: 4`; `research_run.deadline_s: 2700` | **5–45 min**, at most **3×/day** |
| scheduled Claude decision | `research.slots: ["08:30","16:00"]` Gulf = **04:30 / 12:00 UTC** | up to **16.5 h**, mean wait ~6 h |
| news corroboration | two-source rule; `news_event.corroborated_only: true` | as slow as the second source — **not a crash response** |
| human | console on 127.0.0.1; `telegram.chat_id: 0` (unconfigured) | **unbounded** |

**There are exactly two sub-minute actions in the system and both are deterministic**: the exchange
stop and the NAV stop in `loop_tick`. Everything model-mediated is ≥ 15 minutes.

**The cron cadence is not the problem.** Running the whole tripwire overlay at +1h execution instead
of next-bar-open changed BTC −25%/7d from ΔCAGR +1.43 / ΔMaxDD +5.95 to **+2.00 / +5.75** —
indistinguishable, and slightly better. The 5–25 minute path that already exists is fast enough.
Do not build a streaming path.

**What delay actually costs** — 146 fires, bps relative to acting at the next bar's open; positive
means the delay sold you a better price:

| act at | all fires: median / mean / p10 | inside a real crash (n=33): median / mean / p10 |
|---|---|---|
| next open | 0.0 / 0.0 / 0.0 | 0.0 / 0.0 / 0.0 |
| +1h | +11.9 / +5.1 / **−117.7** | +0.1 / **−21.2** / **−157.3** |
| +4h | +26.6 / +19.0 / **−160.6** | +6.3 / **−36.4** / **−370.5** |
| +8h | +31.7 / +5.1 / **−235.4** | +15.6 / **−72.5** / **−696.7** |
| +24h | +69.3 / +75.3 / **−393.3** | −24.2 / −56.6 / **−691.0** |

**Delay costs nothing on average and everything in the tail.** Median and mean are flat or positive
because shocks mean-revert. The damage is entirely in the p10: waiting 4 hours costs **3.7%** of the
position in the worst decile of real crashes, waiting 8 hours costs **7.0%**. That is the argument
for a deterministic response, and the only argument for it.

### 3.4 The threshold is the bottleneck, not the clock

Re-anchored on M0 = the first closed hour whose rolling 6h BTC return ≤ −3% — a definition knowable
at the time, rather than 00:00 UTC of the conventional date. n = 22.

| path | median wait after M0 | median loss already suffered | median dd still ahead | **median share of the 7d drop already spent** |
|---|---|---|---|---|
| next scheduled Claude slot (04:30 / 12:00 UTC) | **10.5 h** | **−0.76%** | −15.61% | **5%** (mean 18%) |
| `r24h ≤ −8%` (the shipped `move` 24h threshold) | **11.0 h** | **−6.92%** | −13.75% | **42%** (mean 41%) |

This is the most useful number in the whole study and it is counter-intuitive. **The twice-daily
schedule is not the bottleneck — the threshold is.** A −8%/24h rule must wait for the drop to *be*
8% deep before it can speak; the clock does not. Median wait is the same (10.5h vs 11.0h) and the
scheduled slot arrives having spent **5%** of the week's drop against the rule's **42%**.

Worked examples of the reactive rule arriving too late to be a response: covid Black Thursday fires
at +10h with **−23.99% already gone**; Tesla/BTC halt at +23h with −13.62% gone; Terra at +17h with
−8.42% gone; the tariff shock at +30h with −10.44% gone and only **−0.40%** of drawdown left to
avoid; Oct 2025 at +25h with −8.75% gone and −3.30% left. In **4 of 22 events** the −8%/24h rule
**never fired at all** inside 7 days while BTC fell 6–16%.

And the tripwire in §3.1, honestly: median **41h** before the bottom sounds early until you see that
a median **47% of the peak-to-trough move is already gone** at the fire and a median **−10.8%**
remains. On 2020-03-12 it fired with **53% of COVID already realised**. There is no configuration in
which this system avoids the first half of a crash.

### 3.5 Where news genuinely adds something

**Never for speed.** Measured on the live archive (341 items): restricted to items that arrived
within 3h of publication — the only ones that could be called news — median publication→fetch
**74.5 min, p90 159.8 min**. Per feed: BitcoinMagazine 3.7h (and **fail_count = 15**, dead, nothing
watches it), CoinDesk 8.4h, Cointelegraph 7.9h, TheBlock 21h, Decrypt 28h, FederalReserve 34d,
BitcoinCore / EthereumFoundation / Blockworks 218–688d (archival backlogs, no live value). The
`*/15` poll is not the binding delay; the feeds' own publication lag is. Arrival is bursty, not
continuous: 9 distinct arrival stamps in 14h 8m, with gaps of 224, 263, 15, 15, 30, 15, 30, 90 and a
then-current 165 minutes.

**The one measured end-to-end chain was a false positive that then blocked the next event.**

| hop | timestamp (UTC) | elapsed |
|---|---|---|
| CoinDesk publishes | 2026-09-23T18:59:36Z | — |
| Cointelegraph carries the same story | 2026-09-23T20:43:56Z | +1h44m |
| ingest RSS poll fetches both | 2026-09-23T21:45:02Z | **+2h45m** |
| corroborate, keyword class `hack`, `corroborated=1` | same cycle | +0s |
| post-ingest scan creates `sig-…-news_event`, `fast_path=1` | 21:45:02Z | +0s |
| guards pass, detached research run spawned | ~21:45:03Z | +1s |
| `flags` stage (reg-watch) | 21:47:54 → 21:48:19Z | +2m52s |
| `decide` finishes, proposal written | 2026-09-23T21:49:34Z | **+2h49m58s from publication** |
| what it said | `abstain: true`, `module: hold` | nothing changed |

The headline was *"Former **Hack VC** partner Hsin-Ju Chuang found dead at 37"*.
`news.event_keywords.hack = [hack, exploit, stolen, breach]` matched a venture firm's name; two
feeds carried the obituary so `corroborated=1`; `hack` is in
`news_event.fast_path_events`, so it skipped the screener and fired a full tier-4 decide run. The
**next** fast-path news signal, 2h15m later, is recorded `status=blocked,
status_reason=cooldown,stale_data` — `signals.planner.cooldown_hours: 4` means any decide in the
last four hours blocks the planner. **An obituary consumed the crash budget.**

**Where news does add something the numbers cannot give:**

1. **The binary venue facts.** A USDT depeg, a symbol halt, a delist notice. Every price in the
   system is USDT-quoted, so a depeg *contaminates the unit of account* — a 2% USDT discount makes
   BTC/USDT print 2% higher with no change in BTC's value and silently rescales NAV, the limits, the
   stop distances and the benchmark at once (`venue-guard` SKILL.md; `crypto-research.md` §1.6). No
   amount of USDT-quoted price data can see this. But the *authoritative* source is `exchangeInfo`
   and the pair quote, not RSS — which is arithmetic, not news, and belongs in an ingest phase.
2. **Idiosyncratic vs market-wide.** The Bybit hack is the one event of 22 where pairwise
   correlation *fell* (0.729 → 0.603) and where breadth was mildest. Classifying "one venue" versus
   "the market" changes whether the response is per-name or book-wide, and a headline is the
   cheapest way to know.
3. **Telegraphed vs instant, for posture not timing.** Telegraphed events are deeper and slower
   (BTC +7d −13.93% vs −9.85%) and hit breadth harder (98.8% vs 90.2%) but do **not** punish alts
   relative to BTC (−0.81pp vs −4.40pp). That changes how much of the response should land on the
   satellite sleeve. It does not change when to act.
4. **Scheduled prints,** which are a calendar, not news, and which `event-blackout` already owns.

Everything else news is asked to do here, it does 75 minutes to 3 hours late, and once every so
often it does it wrongly and spends the day's model budget on an obituary.

---

## 4. How it comes back, and what that costs

### 4.1 The rule

**Re-enter on `min 72 hours` AND `trend re-established`** — close above its 48h EMA and above its
level 24h earlier.

26 episodes classified by recovery 168h after the bottom: 11 L-shaped, 10 U-shaped, 5 V-shaped.
Median net round-trip of a de-risk, 20 bps charged; positive means de-risking helped.

| re-entry rule | median hours out | median % above the low it buys | L (n=11) | U (n=10) | **V (n=4)** | wins |
|---|---|---|---|---|---|---|
| time 24h | 24 | +9.3% | +5.33 | +2.01 | **−0.71** | 17/25 |
| time 72h | 72 | +8.0% | +5.03 | **+8.12** | −1.84 | 18/25 |
| **time 168h** | 168 | +8.1% | **+8.73** | +5.05 | **−6.82** | **19/25** |
| vol ≤ 30d median, min 24h | 123 | +8.5% | +4.86 | +4.78 | −2.90 | 17/25 |
| **trend re-established, min 24h** | 65 | +9.1% | +3.68 | +5.16 | **−1.77** | 17/25 |
| breadth_up ≥ 0.55, min 24h | 28 | +9.6% | +3.71 | +2.97 | −1.72 | 17/25 |

**No rule catches the bottom.** Every one buys back **8.0–9.6% above the low**. Any rule claiming
otherwise is overfitted.

The 7-day clock is the best rule in L-shapes (+8.73%) and the worst in V-shapes (−6.82%) — it loses
4–9× as much as the fast rules exactly when it is wrong. Trend re-establishment gives up about half
the L-shape gain (+3.68%) to cut the V-shape loss to −1.77%, and it is one of only **two** rules
whose holding period moves in the right direction with the shape: 61h in L vs 39.5h in V (the other
is the vol-normalised rule, 128h vs 76h). **Reject breadth-recovery re-entry**: it holds out 35.5h
in V-shapes and 29h in L-shapes, i.e. backwards — it re-enters fastest precisely when it should
wait.

### 4.2 Why the tier cannot be chosen per event

**The shape is not predictable at the fire.** Median values of each candidate discriminator, at fire
time:

| at fire time, median | L (n=11) | U (n=10) | V (n=4) |
|---|---|---|---|
| funding percentile of 1y | 0.56 | 0.60 | 0.36 |
| 30d implied avg pairwise corr | 0.52 | 0.64 | **0.72** (wrong way) |
| BTC above its 200d MA | 7/11 | 5/10 | 3/4 |
| breadth_dn8 | 0.78 | 0.81 | 0.72 |

Correlation is *highest* at the fire in the V-shapes and breadth is *mildest* — both backwards from
intuition. There is no measured input that says "this one bounces". The V-shape numbers rest on
n = 4 and are a hypothesis, not a result.

### 4.3 What re-entry costs, priced

* **~9% above the low**, every time, unavoidably.
* Time out of the market: the recommended trigger is out of full exposure **33.6% of all hours** at
  a 7-day hold, **16.9%** when gated on the 200d MA, **6.6–10%** with conditional re-entry.
* Round trips: **12.9 de-risk cycles a year**, one alarm every **19 days**, **14.4 false alarms a
  year**.
* Fees: 0.65–1.94%/yr of NAV for a −25% tier, 1.29–3.88% for −50%, 2.59–7.77% for a flatten.
  At $1,000 NAV that is **$6.50/yr** (BTC, −25%) to **$38.80/yr** (8-name book, −50%); at $10,000,
  $65 to $388. A flatten on the wide book eats **65% of the annual `risk.max_fee_pct_per_month`
  budget on crash responses alone**.
* These fee figures are **floors**. Costs are modelled flat at 10/30 bps per side from a calm-market
  book walk, and a de-risk fires precisely when spreads widen.

### 4.4 Two clocks that re-entry does not have to fight, and one it does

* **`risk.max_avg_pairwise_corr: 0.70`** is measured on **60 daily** observations
  (`earn_base.RISK_WINDOW_DAYS = 60`). The 1h correlation spike lasts a median ~350 hours, but the
  project's own measurement says **60-day alt correlation does not rise in drawdowns** (0.412 in
  drawdown vs 0.486 calm, `wide-universe.md` §2.1). So `corr_cap` will not block re-entry — and by
  the same token it does not protect on the way down. It is **inert in a crisis in both
  directions**, which is worth stating plainly rather than leaving as a false comfort. Same for
  `risk.max_beta_to_btc: 1.30`.
* **The volatility clock is real.** Volume stays above 1.5× normal for a median **~100 hours after
  the low**, and in the running profile `max_atr_pct: 0.070` suppresses entries while 1h ATR exceeds
  7% of price — which in a crash runs at 15.3× the trailing-30d median hourly range. So the profile
  will throttle re-entry for days after the low, per-pair, as a side effect of a tuning parameter.
  That is *accidentally* the right direction, and it is the only volatility-aware entry brake in the
  running system. **The shipped configuration has none at all.**

---

## 5. The gaps in the current code

### 5.1 Would the system keep buying into a collapse today?

**Yes — up to `risk.max_trades_per_day: 4` entries per sleeve per Gulf day, until NAV crosses
`risk.daily_loss_stop` and the flatten fires 4–14 hours into the crash day.**

Nothing in the 26-check `CHECK_ORDER` reads a price, a market-wide return, breadth, funding, open
interest or realised volatility. **Only NAV.** And three of the checks that sound like exposure
controls get *looser* as the market falls:

* `gross_cap` (0.80), `weight_cap`, `usdt_floor` (0.20) are all measured against NAV. Cash is fixed
  while positions fall, so gross/NAV **falls**: a book at 0.60 gross that drops 20% measures 0.545.
  **The system's "am I too exposed" checks free up headroom exactly as the market falls.**
* `beta_cap` (1.30) and `corr_cap` (0.70) — the two checks nominally built for this — are measured on
  60 daily observations and are **entry-only**; nothing re-measures a held book. Per §4.4 they do
  not move in a drawdown.

What *does* limit buying into a collapse: `max_trades_per_day: 4`, `max_orders_per_day: 16`,
`max_turnover_pct_per_day: 0.50`, the profile's `min_entry_spacing_min: 300`, the profile's
`max_atr_pct: 0.070`, and the daily/monthly locks **after** they fire. Of those, only `max_atr_pct`
knows anything about volatility, it is per-pair on a 1h window, and it belongs to a profile whose own
header calls itself *"a plumbing test, not an edge test."*

### 5.2 Worst-case delay from "the world changed" to "the system did something"

| path | delay | what it achieves |
|---|---|---|
| exchange stop (LIVE) | seconds | one position, at −`stoploss.fixed_pct` |
| NAV stop, evaluation | **~5 s** | — |
| NAV stop, **threshold** | **+4h to +14h** into the crash day (measured, 7 shocks, 80% gross) | flatten |
| NAV stop, fill | + up to **60 min** (`unfilledtimeout.exit: 20` × `exit_timeout_count: 3`) before `emergency_exit` escalates to market | — |
| **deterministic total** | **4h05m – 15h00m** | flatten |
| news → proposal, measured | **2h 50m** | a proposal |
| + `planner.cooldown_hours: 4` if any decide ran recently | **+4 h** (observed live) | — |
| + `planner.max_per_day: 3` exhausted | **+ rest of day** | — |
| + event lands after the 16:00 Gulf slot with no signal path | **+ up to 16.5 h** | — |
| **model-path worst case** | **~21 h** | a proposal |
| **model path in the running system** | **∞** | nothing — see below |

**The sharpest gap is that the model path is currently disconnected.** `config/profiles/fast-test.yaml`
sets `sleeves.a.strategy: SleeveFast` **and** `sleeves.b.strategy: SleeveFast`, so SleeveB does not
read proposals at all. All three proposals on record are `abstain / module: hold`. Every
decision-path latency above is untested end to end in the live system.

So today the honest answer is: the **only** working, no-human path from a market shock to a
reduction in exposure is the per-trade stop and the −3% NAV daily stop. And per §0, the second of
those is the policy that measured worst of anything tested.

### 5.3 Structural gaps, with the code location

| # | gap | location |
|---|---|---|
| **G1** | **`news` is a source in the gate's staleness clock.** `data_age_minutes` takes `max()` over every source in `freshness.json` and discounts only `candles_*`; `ops.lib.freshness.SOURCE_NEWS = "news"` is a canonical source and ingest stamps it, but `news_items.fetched_at` only advances when a *new* item is inserted. So a quiet news cycle ages the "market data" clock. Measured live: news age 164.7 min against `risk.staleness_minutes: 30` → `data_stale` flag active, `block_entries`, scope ALL, 121 `gate_decisions` rows reading `blackout:data_stale`, **622 of 848 elapsed minutes (73.3%) with entries blocked on news quietness alone**. The inversion is the point: in a real crash the feeds flood, news age goes to ~0, and the staleness check becomes **more permissive exactly when the market is least tradeable.** | `strategies/riskgate.py:557` `data_age_minutes`; `ops/lib/freshness.py:46`; `runs/ingest.py` |
| **G2** | **`near_stop` — the only fast path a no-news crash has — is dead.** `compute_hardcase_flags` reads `nav_daily` (written by `nav_job`, cron 00:10 Gulf, once a day). `nav_daily` is empty in the live journal, `if row and anchors` swallows it silently, and `near_stop` can never be True. `nav_points` has fresh rows every `*/15` and is not read. Even once `nav_daily` fills, it will be up to 24 hours stale. | `runs/router.py:269-294` |
| **G3** | **No flag severity reduces exposure.** `SEVERITIES = ("block_entries", "freeze_tier1", "info")`, and `LoopActions` carries `flatten: bool` and nothing else. **Tier 1 is expressible today; tier 2 — the only selling tier with any measured support — has no mechanism at all; tier 3, the one that makes drawdown worse, is the only one the loop can express.** | `ops/lib/flags.py:24`; `strategies/riskgate.py:734-742` |
| **G4** | **`venue-guard` and `event-blackout` are bound to nothing and invoked by nothing.** `runs/research_run.py` hard-codes `skills=["reg-watch"]` and `skills=["crypto-brief"]`; `earn.yaml: skills.bindings` lists only those two; `config/skills-registry.auto.yaml` is `bindings: {}`. `runs/features/venue.py` is imported only by skill scripts and one console service. **Nothing in any cron job ever checks whether USDT is worth a dollar or whether a symbol is halted** — while both SKILL.md bodies claim bindings that do not exist ("Bound to `scanner` (every cycle, before any order can be proposed)"). | `runs/research_run.py:173,204`; `config/earn.yaml:440-445` |
| **G5** | **The scanner computes no breadth at all.** `Features.globals` and `CHEAP_KEYS` carry no cross-sectional aggregate. But `ret_24h` **is** a `CHEAP_KEY` for every watchlist pair, so `breadth_dn8` is one line over data already in memory every 5 minutes. Nothing new to fetch. | `runs/signals/features.py:61` |
| **G6** | **`exit_signal` is not a risk exit.** `RISK_EXIT_REASONS` and `RISK_EXIT_PREFIXES` do not cover it, so the trend-loss exit (`SleeveFast.populate_exit_trend`, `exit_on_trend_loss: true` — 27 of 92 trades in the 30-day backtest) is gated by `EXIT_CHECK_ORDER = ("orders_per_day","turnover_day","fee_budget")`. The profile's own evidence puts `max_fee_pct_per_month: 0.01` at exhaustion in ~22 days at this cadence — after which **the gate refuses exactly the exits that reduce risk**, on the day when every pair loses its 1h trend at once. | `strategies/mechanics.py:43-49`; `strategies/riskgate.py:945-968` |
| **G7** | **A partial de-risk would be throttled by its own order caps** unless its reason carries a `risk_stop` prefix. Trimming 8 positions is 8 orders against `max_orders_per_day: 16`, so a second alarm the same Gulf day, or one landing on a rebalance day, is silently blocked. | `strategies/mechanics.py:49`; `riskgate.py:945` |
| **G8** | **One 4-hour planner cooldown is shared by every trigger class**, and `planner.max_per_day: 3`. Measured: a false-positive obituary at 21:45Z blocked the 00:00Z fast-path signal. Three events in a day and the fourth is refused whatever it is. | `runs/triggers.py:155-165` |
| **G9** | **The watcher's escalation has no consumer.** `escalated=1` fires no research run, writes no flag, alerts nobody; `grep watch_events` outside `runs/watch/` returns only tests. `runs/watch/guard.py` enforces that by design (`ops.lib.flags` is in `FORBIDDEN_IMPORTS`, `WRITABLE_TABLES = {"watch_events"}`). | `runs/watch/store.py`, `runs/watch/guard.py:52-63` |
| **G10** | **KILL does not flatten.** It blocks entries and cancels resting entry orders; `flatten_pending()` returns only `risk_stop_monthly` / `risk_stop_daily`, and `enforce(flatten=True)` is an explicit human argument. | `ops/lib/kill.py:103-134` |
| **G11** | **Two things still hard-wired to a two-asset world.** `RegFlag.asset: Literal["BTC","ETH"] | None` — with 31 pairs a delist notice for any other coin can only be `scope: ALL` (block everything) or nothing. And `news.asset_keywords` covers only BTC and ETH: live, 240 of 341 items have no asset, 79 BTC, 21 ETH, **0 for the other 29 pairs**. | `schemas/flags.py:48`; `config/earn.yaml:435-437` |
| **G12** | **A delisting takes up to 7 days to become actionable.** `universe.exit_only` is written only by the Sunday universe refresh (`cron 0 18 * * 0`) and baked into `config/riskgate.json` at generation time, then needs a container restart — while `reg-watch`'s own body promises a halt flag "moves a held position to exit_only". It does not. | `ops/gen_freqtrade_config.py:291-316` |
| **G13** | **Event-driven decisions bypass the autonomy gate.** Cron research runs go through `ops.autonomy run research_run` (and were being refused: `level_below_required`), but `TriggerEngine.fire` spawns `envwrap.sh research -- python -m runs.research_run` directly. | `runs/triggers.py:199-208` vs `ops/crontab` |
| **G14** | **Two silent operational failures.** The funding phase aborts on the first pair without a perp (`400 … premiumIndex?symbol=PEPEUSDT`), leaving **16 of 31 pairs with no funding or OI, ever** — the one input measured to predict drawdown. And nothing reads `feed_state.fail_count`, so BitcoinMagazine has failed 15 consecutive times in silence. | `runs/ingest.py:256-280,301` |
| **G15** | **`telegram.chat_id: 0`.** Every "a human decides" path in this design has unbounded latency. | `config/earn.yaml:453` |
| **G16** | **The `move` detector cannot distinguish 30 copies of one event from 30 events.** On the real 31-pair whitelist `move 24h ≤ −8%` fires 25.5×/day, reached 30 of 31 simultaneously, exceeded 5-at-once on 58 days in 12 months; on 2025-10-11 07:00, **23 of 31 were down more than 15%**. `DEFAULT_MAX_PER_DETECTOR = 3` in `detectors.py` already stops it flooding the queue — the docstring says so explicitly, and credit where due — but the 3 it contributes are still 3 copies of one market event, and they still consume `validator.max_per_day: 6` slots that the fast-path `hack`/`depeg`/`delist` class is supposed to get. | `runs/signals/detectors.py:71` |
| **G17** | **Timeframe drift.** Every committed file says 4h (`config/freqtrade-{a,b}.json:84`, `config/earn.yaml: trading.timeframe`), and the running bots are on the `fast-test` profile at 1h, selected in the **runtime** deployment only (`profiles.active` is null in the committed `earn.yaml`; the comment at line 544 explains why — selecting it breaks ~108 tests). Every latency in §3.3 is quoted against a 1h closed bar. And `crypto-research.md` §1.9 measures that 4h over 1d buys nothing and costs double (Sharpe 1.13 vs 1.14, fee drag 2.6%/yr vs 1.2%/yr, worse drawdown) — 1h doubles it again. | `config/profiles/fast-test.yaml`; `config/earn.yaml:544` |

---

## 6. The build list

Ordered by measured value per unit of risk introduced. Each item names what justifies it, and
whether it belongs in the **deterministic** gate (fast, unarguable, no model) or the **model** tier
(slow, judgement, needs context). **A crash response that waits for a model call is not a crash
response**, and nothing in the deterministic column may ever sell.

### DETERMINISTIC — fast, no model, in `runs/ingest.py`, `runs/signals/**` or the gate

**1. Split the freshness clock.** `data_age_minutes` must compute market-data age from
`book_snapshots` and `candles_*` only, and expose news age as a separate, non-blocking number.
Keep fail-closed for market data; a missing or unparseable news stamp must not block anything.
*Justified by:* G1 — 73.3% of live uptime with entries blocked on news quietness, and a staleness
check that becomes **more permissive during a crash**, which is the wrong direction on the one
question this document exists to answer. This is the highest-value item on the list and it is a
few lines. `strategies/riskgate.py` — tier 2.

**2. Compute breadth as a global feature.** `breadth_dn8` = share of eligible pairs (≥30d history)
with `ret_24h ≤ −8%`, plus `median_ret_24h` and `z24` for BTC. Every input is already in
`Features.pairs` as a `CHEAP_KEY` for every watchlist name; nothing new is fetched and the cost is
one pass over a dict the scanner already builds every 5 minutes.
*Justified by:* G5 (no cross-sectional aggregate exists) and §3.1 (breadth is one of only two
families with any measured lift, and it is **the only input that sees the alt-vs-BTC gap, which is
where the permanent loss is** — §1.5). `runs/signals/features.py` — tier 2.

**3. A `market_shock` flag that can ONLY tighten.** Fire on `z24 ≤ −2.0 AND breadth_dn8 ≥ 0.30`
expressed as **rolling percentiles**, not fixed thresholds. Severity `block_entries`, scope ALL,
bounded expiry of 48h, written through `ops.lib.flags` from the scanner or an ingest phase. It must
be structurally incapable of causing an exit.
*Justified by:* §3.1 (P(fwd 96h dd ≤ −8%) 0.240 vs 0.120 base, 25/26 episodes, two independent
searches agreeing) and §2 tier 1 (entering inside the window buys 14–30% more variance for the same
or lower return; at 336h the mean is −0.01% in-flag against +2.55% out). Percentiles rather than
thresholds because the split-half ratio holds (1.77 → 1.32) while the level compresses — the funding
pattern. **Ship with the false-alarm rate on the console**: ~19 alarms/yr, ~14 of them false, one
every 19 days. `runs/signals/**` + `config/**` — tier 2.

**4. Add a `reduce_exposure` severity that carries a multiplier — the mechanism, not the trigger.**
A severity holding a target-weight multiplier in [0,1] plus an expiry; the gate applies
`min(proposed_weight, multiplier × current_weight)` and refuses any increase while it is live, with
the multiplier bounded in config so no model can widen it.
*Justified by:* G3 — tier 1 is expressible, tier 2 has **no mechanism at all**, and tier 3 (the one
that makes drawdown worse) is the only one `LoopActions` can reach. Build the mechanism because it
is also what a proportional fix to the daily stop needs. **Do not arm it to sell** until the
incremental test in item 10 is run. `ops/lib/flags.py`, `strategies/riskgate.py`,
`schemas/flags.schema.json` — tier 2.

**5. Peg and halt as an ingest phase, with no model in the path.** Call `runs/features/venue.py`
directly every 15 minutes and write `depeg` / `symbol_halted` flags. It is pure arithmetic over
`exchangeInfo` and the pair quote.
*Justified by:* G4 — nothing automated ever asks whether USDT is worth a dollar, while two SKILL.md
bodies claim it does. And §3.5(1): a depeg is a measurement failure that silently rescales NAV, the
limits, the stops and the benchmark at once, and no USDT-quoted price can see it. `runs/ingest.py` —
tier 2.

**6. Point `near_stop` at `nav_points`.** Read the freshest row within ~30 minutes; fall back to
`nav_daily` only when there is none. Add a test asserting `near_stop` becomes True when a
`nav_points` row crosses `daily_loss_stop − stop_proximity_pct`.
*Justified by:* G2 — the only fast path a no-news crash has is dead, and dead **silently**.
`runs/router.py` — tier 2.

**7. Make `exit_signal` unblockable, or at minimum never let `fee_budget` block a position-reducing
exit.** A new "protective" class between discretionary and risk.
*Justified by:* G6 — 27 of 92 trades exit that way, the fee budget exhausts in ~22 days at the live
cadence, and a crash is exactly when every pair loses its trend at once.
`strategies/mechanics.py` — tier 2.

**8. Give any de-risk order a `risk_stop`-prefixed reason** (e.g. `risk_stop_tripwire`), with one
test asserting `is_risk_exit()` is true for it.
*Justified by:* G7 — 8 trims is 8 orders against `max_orders_per_day: 16`.
`strategies/mechanics.py` — tier 2.

**9. Asymmetric blackout window, −360 / +480 minutes.** Replace the symmetric
`risk.blackout.window_minutes: 60`.
*Justified by:* §1.6 — 79 FOMC releases, 1.72× the median 4h move, direction t = −0.49 at +24h,
elevation measured T−7h to T+8h with the peak at **T−1h**. SCHEDULED is the **only** class where
advance action is possible, and standing aside is the only action. This is worth more than any new
crash detector, and `event-blackout` already identified it. `config/**` — tier 2, human.

### HUMAN ONLY — change proposals with walk-forwards attached

**10. Raise a change proposal on `risk.daily_loss_stop: 0.03`.** Two independently measured fixes:
widen the trigger (−5% → +4.14% CAGR; −8% → +13.32%, 6.9 exits/yr, 1.10% fee/yr) or keep −3% and
**halve instead of flatten** (+13.05% at the same trigger). Attach both walk-forwards and the
precision row (9.1%, the worst of anything measured).
*Justified by:* §0 and §3.2 — 66.7 fires/yr, 10.66%/yr in fees (89% of the whole
`max_fee_pct_per_month` budget on stop-outs alone), +30.53% → −5.23% CAGR, and a **worse** max
drawdown than doing nothing. Study 2 reached −23.82 ΔCAGR / −1.63 ΔMaxDD by an independent route.
The flatten is what does the damage. Do not touch it from a run; do not read a good paper quarter as
validation (`crypto-research.md` §3: 219 years of power needed).

**11. Run the one missing measurement before arming any selling tier**: a −25% partial de-risk
**incremental to the MA125 trend filter**, walk-forward, costs on, and the `min_notional_usdt: 25`
floor modelled rather than assumed. Resolve the floor per-asset (trim core/major 25%, exit
satellites entirely) or state the NAV below which the tier does not apply (~$2,000 at −25%, ~$1,000
at −50%), with a test rather than a discovery in production.
*Justified by:* §2 tier 2 — the encouraging +1.43 ΔCAGR is measured against buy-and-hold, Study 1's
incremental test cost 15 points of CAGR for 1 point of drawdown, and at live seed size a satellite
trim is a $12.50 order against a $25 floor.

**12. Separate the planner cooldown by trigger class**, and tighten `news.event_keywords.hack` to
word-boundary matching plus a required second keyword.
*Justified by:* G8 and §3.5 — a venture firm's name in an obituary fast-pathed a tier-4 decide run
and then consumed the 4-hour budget so the next fast-path signal was blocked.

**13. Widen `RegFlag.asset` and `news.asset_keywords` from the universe snapshot's base assets**
instead of a two-asset `Literal`.
*Justified by:* G11 — 0 of 341 live news items attributed to the 29 non-BTC/ETH pairs, and a delist
notice for any of them can only be expressed as "block everything".

**14. Reconcile the timeframe, and decide what the `fast-test` profile is costing.** Confirm 1h vs
4h through `ops.gen_freqtrade_config --check` so the drift is visible, and either accept in the
runbook that SleeveB trades no proposals, or drop the `b:` line so one bot keeps trading proposals
while the other is the control.
*Justified by:* G17 and §5.2 — every latency here is quoted against a 1h bar, `crypto-research.md`
§1.9 measures 4h over 1d as buying nothing for double the fee drag, and the entire
news→decide→proposal→order path is currently untested end to end.

### MODEL TIER — slow, judgement, and never in the critical path

The model's crisis job is everything that is **not** time-critical:

* **Whether to stay tightened** after a deterministic `market_shock` window expires — the one
  question where 96 hours of context beats a threshold, and where a 6-hour wait costs nothing
  because the position is already flat on new entries.
* **Classifying idiosyncratic-single-venue vs market-wide** (§3.5(2): the Bybit hack is the one
  event of 22 where correlation *fell*), which changes whether a response is per-name or book-wide.
* **Classifying telegraphed vs instant** for posture, per §1.6 — alt-vs-BTC gap −0.81pp vs −4.40pp.
* **The post-mortem**, graded on process and never on whether a crash followed.

Nothing in this column may set a flag, size an order or reduce a weight inside an hour. The watcher
stays hand-raise-only (`runs/watch/guard.py`) — if it gets a consumer at all, the narrow exception is
an `info`-severity scoped flag plus a `signals` row, never a position action.

### Two dead ends to record so nobody builds them

* **Gaps do not exist.** Largest hour-to-hour open-vs-prior-close gap across 22 events × 72h:
  **0.190%**; count above 0.5%: **zero in every event** (calm max 0.118%). No gap logic, ever.
* **No candle-derivable liquidity proxy works.** Amihud illiquidity z ≥ 1.5 → 0.117 and
  Corwin–Schultz spread z ≥ 3 → 0.145, against a 0.120 base. The Corwin–Schultz estimate (median
  13.0 bps on 1h bars) is not even measuring the quoted spread, which is 0.0012 bps live — it is a
  range proxy wearing a spread's name. Real spread and depth need the live book, which
  `book_snapshots` is accruing (748 rows) and which cannot be backtested for years.
* **And the third, already known:** volume spike and range expansion are **coincident** with crashes
  (26/26 episodes) and carry **zero predictive lift** (0.124–0.137 against a 0.120 base). Keep
  `volume_spike` as a volatility-regime input; never as a timing signal.

---

## 7. Honest limits

**1. No sub-hourly data, and the cascades happened in minutes.** There is no 15m or 5m feather in
`~/earn-run/data/binance/` — only 1h, 4h, 1d for 108 pairs. The two events the owner asked about
most directly are exactly the ones this bites: the 2020-03-13 BitMEX-outage waterfall and the
2025-10-10 cascade both resolved inside single hourly bars (the 2025-10-10 21:00 bar alone carried
50.9× normal volume). A 15m tripwire would fire up to 45 minutes earlier; that is untested because
the candles do not exist locally.

**2. Liquidation history does not exist free, so every cascade here is inferred.** Re-confirmed as a
constraint: `fapi/v1/allForceOrders` 404, `data.binance.vision/…/liquidationSnapshot/` 404, CoinGlass
paid. The down/up volume split is an intrabar close-position proxy, not taker flow. The real taker
aggressor data is in the bulk archive and was not pulled — that is the single highest-value follow-up
for this question.

**3. Survivorship bias makes every recovery and breadth number OPTIMISTIC.** The 108 pairs are
today's listings; the coins that died are not in the klines API at all. `LUNC_USDT` starts
2022-09-09, *after* the rename, so the original LUNA going to zero is absent from the Terra breadth
row; FTT is absent from FTX. **"0.0% of covid alts still below T0 today" is a statement about
survivors, not about what an investor held in March 2020.** Read every "% still below T0" as a
**floor** on the damage.

**4. Pre-2021 breadth rests on 9 pairs.** 36 of the 108 pairs start at exactly 2021-01-01 — an
ingest floor, not a listing date. For the three COVID events only 9 pairs have 30d of prior history
(BTC, ETH, BNB, LTC, ADA, XRP, TRX, DOGE, LINK). **The single most important event in the sample has
the weakest breadth measurement in it.**

**5. The policy backtest book is deliberately naive, so its absolute drawdowns are NOT Earn's.**
8 equal-weight names at gross 0.80, no volatility targeting, no per-name sizing — which is why max
drawdowns run −75% to −95%. Earn sizes with vol targeting, and the already-measured trend ×
volTarget combination puts MaxDD at −28.6%. **The policy comparison is valid; the levels are not.**
Anyone lifting −94.55% into a report about Earn's risk is misreading it.

**6. Multiple testing is real and both studies are inside it.** Roughly 11 policy variants, 7
triggers and 3 breadth thresholds in Study 1; ~300 configurations (7 triggers × 3 tiers × 7
re-entry rules × 2 assets × 2 latencies) against **26 episodes** in Study 2, and the V-shape
re-entry result rests on **4 episodes**. On 7.73 years the sd of an annual Sharpe estimate is ~0.36,
so the expected best in-sample Sharpe from ~20 zero-skill trials is ~0.9 — the trend filter's 0.65
is **not** outside search noise on its own. It survives here because it is the *shipped* rule being
re-tested rather than a new one being discovered, and because it is a plateau across MA100/125/200
(0.64 / 0.65 / 0.44) rather than a peak. `review.change_gates.walk_forward_min_out_sample_delta:
0.0` would not catch any of this. **The search already killed one apparent winner**: `z4 ≤ −2.5 &
breadth ≥ .3 & below-200dMA, flatten, vol re-entry` showed ΔCAGR +6.98 / ΔMaxDD +12.32 full-sample
and **−10.51 ΔCAGR in 2023-2026**, with its whole neighbourhood at −11 to −27pp.

**7. Event anchors are a judgement call and the answer moves with them.** T0 = 00:00 UTC of the
conventional date; `yen_carry_unwind` was hand-set to 2024-08-04 23:00 because the waterfall
demonstrably began the prior UTC evening, and the other 21 were not audited for the same problem.
This bit visibly once: the 00:00-anchored slot comparison said 9% of the drop was spent at the first
slot, and re-anchoring on the actual start of the move changed it to **5%** with a median wait of
10.5h instead of 4.5h. Both are in §3.4. The +1h/+4h speed columns carry the same sensitivity,
unbounded.

**8. Three of the signals the brief asked for cannot be measured on history at all, and none was
faked.** *Open-interest collapse*: `knowledge/earn.db: open_interest` is empty and every
`fapi/futures/data/*` endpoint retains ~31 days — untestable, so unrecommended. *Spread widening*:
`book_snapshots` holds 748 rows; both candle-derivable substitutes have zero lift. *Venue health*:
`exchangeInfo` has no history — it can only be a live blocker, never a backtested tripwire. Any
future claim about these needs the forward-accruing snapshots, which will take years.

**9. n = 22 (Study 1) / 26 episodes (Study 2), and the class split is lopsided** — 18 INSTANT
against 4 TELEGRAPHED. The telegraphed column in §1.6 is a mean of four events; the −0.81pp
alt-minus-BTC figure in particular rests on Celsius/3AC's +8.41pp outlier. SCHEDULED is not in
either sample — those numbers are the existing 79-FOMC study.

**10. The 2023-03 depeg is cited, not re-measured.** There are no stablecoin pairs in the local data
(no USDCUSDT, no BUSD) — everything is USDT-quoted. The peg numbers come from
`crypto-research.md` §1.6. The *consequence* was verified (BTC +11.4% at 7d, +28.4% at 30d while
equities fell); the depeg itself was not. And because every price here is USDT-quoted, a depeg
week's returns are contaminated by the unit of account — exactly as `venue-guard` warns.

**11. `feb2026_flush` (2026-02-05, BTC −14.02%) is data-flagged with no cause asserted.** It
surfaced from the discovery pass, not from a citable source, and no narrative was attached. It is
the largest BTC daily fall since 2022-11-09 and carries 9.93× normal first-day volume, so whatever
it was, it belongs in the event list.

**12. Costs are flat 10 bps (core) / 30 bps (book) per side, no slippage, no BNB discount.**
Justified for Earn's sizes by the measured book walk (`crypto-research.md` §1.10: 0.001–0.19 bps at
Earn's sizes), but the flatten rows are where it understates: real crash liquidity is worse than the
calm-market book those figures came from, and a de-risk fires precisely when spreads widen. **Every
fee-drag figure in this document is a floor.**

**13. The live runtime sample is tiny and young.** ~14 hours of operation at the time of the audit:
26 ingest cycles, 341 news items, 24 signals, 3 proposals, 15 orders, 0 incidents, 41 `llm_calls`
rows. The 73.3% blocked figure, the news-arrival gaps and the per-feed publication lags all rest on
that one window and a partly cold-start archive. **Indicative, not stable estimates.** And several
absences in §5.3 are argued from repo-wide grep excluding tests, which is weaker than a test.

**14. The crash-timing table in §5.2 is a proxy, not a costed backtest.** It uses BTC/USDT hourly
closes as a stand-in for the book and assumes NAV moves by (gross × market move) with gross constant
through the day. A real 8-name alt book would have fallen **harder** than BTC (median top-30 alt
−34.4% on 2021-05-19 against BTC's −14.4%), so the daily stop would fire **earlier** than the table
says and the "fell further after it fired" column is if anything understated.

**15. Not verified by a test run.** The code findings in §5 are reads of a green tree. Study 2's
workspace reported `earn-test c2 -q tests/strategies tests/test_foundation tests/test_features` →
**991 passed**; Study 3's `earn-test` invocation hung past its timeout and ran nothing. Nothing in
the repo, the runtime copy, the running containers or `knowledge/flags.json` was modified; no `.env`
was read; no `ruff format`; no commit.

---

## 8. What this document does not cover

What makes a coin grow; the market-factor decomposition (how much of any coin's move is "the whole
market went up"); the strategy catalogue ranked by portfolio contribution; and the per-coin
decision-record audit — whether "why this coin, why would it grow, when do I sell into strength,
when do I sell into weakness" is a required, validated, stored field or merely encouragement in a
prompt. Those are separate threads. This one answers only: what happens in a crash, what to do, how
to know, how to come back, and what is broken today.
