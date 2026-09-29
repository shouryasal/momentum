# Dip strategy — does buying deep drawdowns on reliable coins earn its place?

**Status:** audit, decision-grade. Nothing here is switched on until the build list in §9 ships
through its stated tier. No running bot, config, `.env` or `var/state/**` was touched producing it.
No `ruff format`, no commit.

**Why it exists (owner, verbatim):** *"the 20000 put in is worth less, system is not doing its job,
its supposed to earn not loose. so we need to build and improve until we are robust and best."*

**And the correction that triggered it (owner, verbatim):** *"also the things you mentioned dip
buying are not for short term, also they are done on very reliable coins only."*

The owner is right, and the correction is accepted. `growth-audit.md` §2.5 rejected "buy the dip"
on a test nobody runs — deepest drawdowns across the whole eligible universe, ten-day holds. That
was the wrong test. This document runs the right one: **deep drawdowns on assets that are
demonstrably reliable at the time, entered in scaled tranches, held for months.** Two independent
studies were built for it, both on a survivorship-free panel with point-in-time reliability. Every
number below was re-derived on this host on 2026-09-25 from the study scripts, not transcribed from
their reports — and re-deriving them found **two errors that change the conclusion**, both recorded
in §1.3 and §7.

---

## 0. The answer, before the detail

**The correction was right about the shape of the strategy and does not rescue it. Dip-buying a
basket of point-in-time-reliable coins does not earn its place, at any horizon, under any
reliability definition, on any accounting. But re-testing it uncovered a measurement error that was
suppressing the one thing in this project that does work — and with that error fixed, the trend
ensemble on BTC/ETH beats buying and holding BTC on return, drawdown and Sharpe at the same time,
over the longest window the data allows.**

That is the first time in this project's history that anything has beaten hold on all three axes.
It is also not an edge that clears its own statistical bar, and §6 says so in numbers before §8
recommends running it.

### 0.1 The dip hypothesis, decided

| question | answer | the number that decides it |
|---|---|---|
| Are long holds better than short ones? | **Yes** — the owner's correction, measured | Across 6,720 costed configs, 30-day exits are the worst of any exit (median Sharpe **−0.11**, median CAGR **−1.0%**); 365-day exits have the best median CAGR (**+3.7%**) |
| Does asset quality matter? | **Yes, and more than horizon** | Point-in-time strictness moves median max drawdown from **−83.2%** (loosest definition) to **−16.5%** (strictest), and median worst single event from **−90.5%** to **−34.2%** |
| Do deep drawdowns on reliable coins recover? | **Not often enough to pay** | 13 of 20 events (**65%**) never recovered to the prior ATH inside 1,095 days. The 31 point-in-time-reliable names that fell 50% had a median forward 365-day return of **−43.5%** |
| Is "has survived a cycle" a useful filter? | **Almost worthless point-in-time** | Survivors +3.0% median forward 365d vs not-yet-survivors +1.5% at the same dip depth. It is worth **1.5pp**. The rest of the "reliability" effect is just *old, liquid and large* |
| Does it beat holding BTC? | **No** | **0 of 6,720** configs beat BTC's CAGR. In **125 of 128** cells the median dip trade lost to simply holding BTC over its own identical entry-to-exit dates, both costed — cash drag excluded |
| Does it beat the trend filter already shipped? | **No** | **0 of 6,720** beat a BTC MA125 filter on CAGR, Calmar or Sharpe |
| Does it survive out of sample? | **No** | Purged walk-forward: loses to BTC on return in 2 of 3 folds; the BTC-only version returns **7.3% CAGR vs hold's 13.4%** over 1,728 OOS days and sat in cash for all of 2024 while BTC did +120.8% |
| Is the −50% trigger a robust choice? | **No** | A spike, not a plateau. CAGR vs hold by entry level: −0.30 **−34.4pp**, −0.40 **−23.9pp**, −0.50 **−6.3pp**, −0.60 **−22.0pp** |

**Where the recovery premise actually holds is BTC, not "reliable coins".** BTC's own median forward
365-day return is +31.2% unconditionally and **+71.6% after a −50% drawdown, with an 88.7% hit
rate**. The reliable basket at the same depth: **+3.0%, 52.2%**. Spreading the identical signal
across the basket instead of concentrating it in BTC turns **+42.9% CAGR into −8.1%**. The effect
the owner is describing is a BTC/ETH/BNB effect, and `crisis-policy.md` reached the same place by a
different route — core fully recovered in 22 of 22 crash events while 65.1% of the alts in those
same events are still below their pre-event price today.

### 0.2 What would it take to beat holding BTC — the three measured answers

1. **Directional skill we do not have.** Break-even balanced accuracy to merely *match* hold is
   **56.07%** at 1-day holds, **60.69%** at 20 days, **73.70%** at 180 days — equivalently a
   directional rank IC of **0.19 to 0.68**. Measured on BTC across 45 feature–horizon pairs, **not
   one reaches |t| = 2**; the best is `rsi14` at IC **0.123, t 0.7**. We are short by a factor of
   about four, and `above200dMA` has the *wrong sign* at every horizon beyond one day.
2. **Leverage we do not have, and should not want.** Return per unit of exposure: BTC hold 49.2%,
   the dip rule **129.3%** (42.9% CAGR at 0.33 average gross). But levering it with perp funding
   charged (measured: **12.25%/yr per 1× of extra long notional**) gives x2 = **77.8% CAGR at
   −80.9% max drawdown** — it buys return by handing back the entire drawdown advantage and then
   some. The one "levered trend beats hold on both axes" figure quoted in the second study's report
   **does not reproduce** from the surviving scripts (§7, limit 3).
3. **Or stop asking for it on that axis.** A long-only spot book already 100% in BTC **cannot
   express upward conviction at all** — proved mechanically: "BTC core + BTC dip tranches, gross
   capped at 1" returns 49.2% / −76.6% / Sharpe 0.80, identical to buy-and-hold to the decimal.
   Every signal we can build is expressible only as a *reduction* from full exposure. That is why
   everything this project has tested wins on drawdown and loses on return. It is a property of the
   mandate, not of the search.

**And then the fourth answer, which is new and is the reason this document is not another
negative:** reduce exposure *well*, on two assets instead of one, and stop measuring the result
against the wrong window.

### 0.3 The headline table — full panel, the longest window the data allows

2017-08-17 → 2026-09-24 · 3,326 days · 9.11 years · costs on at 15 bps/side · cash earns 0% ·
every signal lagged one full day · Sharpe = CAGR / annualised vol

| book | CAGR | vol | Sharpe | MaxDD | avg gross |
|---|---|---|---|---|---|
| BTC buy-and-hold | 38.6% | 66.9% | 0.58 | **−83.2%** | 1.00 |
| BTC trend ensemble (15 variants) | 39.5% | 37.5% | 1.05 | −44.5% | 0.46 |
| **BTC/ETH trend ensemble** | **43.1%** | 39.9% | **1.08** | **−45.6%** | 0.45 |
| ensemble OR dip(−50%) | 48.0% | 52.0% | 0.92 | −75.3% | 0.72 |
| ensemble OR dip(−50%, confirmed > MA50) | 31.9% | 44.6% | 0.71 | −76.3% | 0.61 |

The BTC/ETH trend ensemble beats buy-and-hold BTC **on return (+4.5pp), on risk-adjusted return
(1.08 vs 0.58) and on worst drawdown (−45.6% vs −83.2%) simultaneously** — and it does so while
being handicapped at the start of the window, because the 250-day moving averages read flat for the
first 250 bars and the book therefore captured **+47.0%** of BTC's **+220.1%** rally in late 2017.
The win is achieved *against* that handicap.

It has **no fitted threshold**: fifteen trend variants, equal-weighted, averaged into one book
traded once, on the two assets already in `universe.core`. Nothing was selected.

### 0.4 Which side of the trade-off — plainly

`growth-audit.md` §5 and `crisis-policy.md` §0 both said: this project wins on drawdown and loses on
return. **That is now true of the dip family and no longer true of the trend family.**

| window | BTC hold | BTC/ETH trend ensemble | verdict |
|---|---|---|---|
| 2017-08 → 2026-09 (full) | 38.6% / −83.2% / 0.58 | **43.1% / −45.6% / 1.08** | wins all three |
| 2019-01 → 2026-09 | 49.7% / −76.6% / 0.81 | 49.3% / −45.6% / 1.19 | ties return, halves drawdown |
| 2021-01 → 2026-09 | 20.4% / −76.6% / 0.36 | **33.9% / −40.3% / 0.89** | wins all three |
| 2019-22 | 45.3% / −76.6% / 0.62 | **67.3% / −45.6% / 1.37** | wins all three |
| 2023-24 (clean bull) | **137.6% / −26.2% / 2.82** | 53.7% / −28.3% / 1.47 | **loses 84pp of return** |
| 2025-26 (chop) | −6.1% / −53.0% / −0.14 | **11.0% / −26.0% / 0.46** | wins all three |
| last 12 months | −25.9% / −53.0% / −0.57 | **−0.9% / −22.0% / −0.04** | wins all three |

**The honest reading of that table is not "we found alpha". It is "we found a book that is
consistently second-best and never last."** It gives up 84 points of CAGR in a clean BTC bull market
— the modal way this whole approach fails — and it wins everywhere else. Whether that is worth
owning is a preference, and §8 states the preference it implies and prices it.

### 0.5 The owner's $20,000, measured

Costs on. "Worst mark" is the lowest the account would have printed inside the window.

| book | last 12 months | worst mark | 2025-01 → now | worst mark |
|---|---|---|---|---|
| BTC buy-and-hold | **$14,823** | $9,406 (−53.0%) | $17,949 | $9,406 |
| BTC/ETH hold 50/50 | $13,975 | $8,014 (−59.9%) | $17,553 | $8,014 |
| BTC trend ensemble | $19,912 | $16,226 (−18.9%) | $20,685 | $15,561 |
| **BTC/ETH trend ensemble** | **$19,830** | **$15,593 (−22.0%)** | **$23,964** | **$14,804** |
| ensemble OR dip(−50%) | $23,979 | $15,710 (−21.4%) | $24,911 | $15,066 |
| dip −50% alone (BTC) | $27,467 | $17,677 (−11.6%) | $27,467 | $17,677 |

**So the first thing to say to the owner is that the $20,000 is worth less because BTC is worth
less.** BTC is **−25.9% over the last twelve months** and **−32.6% below its own 365-day high as of
2026-09-24**. This was not a period in which any long-only spot book made money holding the market.
The second thing is that the two rightmost books in that table — the ones that were mostly in cash —
would have ended the year *up*. That is the whole prize, it is real, and it is a drawdown prize
being paid out in a falling market.

---

## 1. How the panel was built, and the three traps in it

### 1.1 The panel

`~/earn-panels/panel_1d.parquet`: **841,491 daily candles, 747 USDT tickers that ever existed**,
2017-08-17 → 2026-09-24, dead coins included (282 no longer trade). Plus `wk.parquet` (49,833
coin-weeks with features at T and forward 7/30/90d returns) and `funding.parquet` (2,567,781 prints,
653 perpetuals, from 2019-09-10). Both studies built their own derived panels from it.

Study 1 split ticker reuse into separate series: **771 series, of which 274 end more than 30 days
before the panel end**. After removing leveraged UP/DOWN/BULL/BEAR tokens, stables, tokenised
equities and wrapped/PAXG (180 series), **591 tradeable series** remain.

### 1.2 Trap one — the LUNA symbol reuse, and the inherited split rule that got it backwards

`growth-audit.md` already records that `LUNAUSDT` was reused for LUNA 2.0 on 2022-05-31, producing a
**177,399× one-day "return"**. The rule it left behind — split where the ratio against the
**previous row** exceeds 8× or falls below 1/8× — is wrong, and study 1 found out why:

* it **split** LUNAUSDT on 2022-05-11 and 2022-05-12, which were *real* −94% and −99.97% price days
  for the same token;
* it **missed** the actual reuse on 2022-05-31 (0.00005 → 8.87 across an 18-day trading gap),
  because on a wide index `shift(1)` lands on a NaN.

Under that rule **the worst outcome in the whole study disappears.** The replacement rule works on
the last *valid* observation: split where the ratio > 8, or where a gap > 7 days comes with a ratio
> 2 or < 0.5. It catches **24 breaks across 21 tickers** — LUNAUSDT 2022-05-31 (Terra 2.0), COCOS
2021-01-23, DREP 2021-04-02, STRAX 2024-03-28, VIDT 2022-11-09, VEN→VET 2018-10-19, plus 18
leveraged-token rebases. LUNAUSDT segment 0 now correctly ends at 0.00005 on 2022-05-13, **−99.99996%
from its ATH**.

**This belongs in `tests/`, not in a document.** It is build item 6 in §9.

### 1.3 Trap two — a capital-accounting bug that made tranching look like alpha

Study 1's first portfolio simulator returned reserved-but-unfilled tranche cash to the cash balance
at exit, although it had never been withdrawn at entry. It minted money **in proportion to how much
capital the strategy reserved and did not deploy** — which is to say, in proportion to exactly the
thing a tranche ladder does.

| config | before the fix | after the fix |
|---|---|---|
| R_soft / −70% / 4 tranches / 180d | **+2,349% total, CAGR 51.2%, MaxDD −21.1%, Calmar 2.43, Sharpe 1.91** | **+12.6% total, CAGR 1.5%, Calmar 0.05** |

Every figure in this document is post-fix, with an assertion that cash never goes negative. **The
existence of that bug is the reason to distrust any favourable tranche result anywhere — including
in this document — unless the reserved-cash treatment is stated explicitly.** Ours is: reserved cash
earns 0% and stays in the equity denominator.

### 1.4 Trap three — a signal warm-up inside the measurement window, which was hiding the answer

**This is mine to report and it is the most consequential correction in the document.** Study 2 built
a derived panel (`~/dp2/rel.parquet`) that starts on 2019-01-01. Every moving average and Donchian
channel was therefore computed *inside* the measurement window, so the trend ensemble was forced flat
for roughly the first 250 bars of 2019 — a period in which BTC went from about $3,700 to $10,000.

Recomputed with the warm-up outside the window (signals from the full panel, measurement from
2019-01-01), on identical code:

| BTC trend ensemble, 2019-01-01 → 2026-09-24 | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| warm-up **inside** the window (as study 2 reported) | 41.26% | 1.12 | −43.55% |
| warm-up **outside** the window (correct) | **47.04%** | **1.21** | −43.55% |

The drawdown is identical to two decimals; the return is understated by **5.8pp of CAGR**. That one
artifact is the difference between study 2's headline framing — *"you pay roughly 8pp of CAGR to
remove 33pp of drawdown"* — and the true trade, which is **you pay about 2.7pp of CAGR to remove
33pp of drawdown**. On two assets instead of one you pay nothing at all.

Every ensemble number in §0.3, §0.4 and §8 uses the corrected footing. Study 2's own tables are left
as they were measured and are labelled where they differ.

### 1.5 Conventions, and one convention clash worth naming

* Costs **15 bps per side on every weight change**, always on: 0.30% round trip. Signal read at the
  close of day *t*, filled at the close of *t+1*.
* Cash earns **0%**, and sits in the equity denominator. Every return, drawdown, Calmar and Sharpe
  is on **total capital**, never on the deployed tranche.
* Delisting is marked at the **last available close**, which is optimistic — it assumes you sold at
  the final print rather than being frozen in a halted book.
* **The two studies use different Sharpe numerators.** Study 1 annualises the *arithmetic mean* of
  daily returns (BTC hold reads **0.64**); study 2 and this document use **CAGR / vol** (BTC hold
  reads **0.80** over 2019-2026, **0.58** over the full panel). For a 61%-vol asset that gap is
  large. Comparisons are only ever made **within** one convention, never across, and every table
  says which it is.
* A first-bar convention accounts for a further ±0.5pp: BTC hold over 2019-2026 is **+2,167.9% /
  49.70%** including the 2018-12-31 → 2019-01-01 return, and **+2,111.6% / 49.2%** with the first
  day's return zeroed. The brief's "+49.3% CAGR, −76.6% MaxDD" reproduces exactly on the second
  convention; the +194% over 2021-2026 reads +186.3% / +190.3% depending on the same choice.

---

## 2. Point-in-time reliability — how few names there are, and what the age bar buys

### 2.1 The definitions, and the fact that they contain corpses

Reliability is computed from information available at T only: **(a)** age in trading days observed;
**(b)** liquidity rank by trailing-90d median dollar volume; **(c)** cycle survival — the number of
*completed* episodes strictly before T in which the name fell ≥50% from its then-ATH and then printed
a **new** ATH. Study 1 swept 14 definitions; study 2 specified one (age ≥ 1095d, ADV ≥ $10M, top 20
by liquidity, one survived-and-recovered 50% drawdown) plus two controls.

**The proof that these rosters are not hindsight is that they contain corpses.** A roster picked with
today's knowledge would contain none of these:

| definition | names ever qualifying | of which later died or were renamed |
|---|---|---|
| `R_core` (age ≥ 3y, top-10 ADV, 1 survived cycle) | 25 | 2 — FTM→S, MATIC→POL |
| `R_wide` | 38 | 5 — BLZ, FTM, MATIC, OM, WAVES |
| `R_soft` | 55 | 9 — adds BTT, MKR, OMG, RNDR |
| `L10` (liquidity only, no age bar) | 58 | 8 — adds BCC, BCHABC, EOS, SXP, **LUNA** |
| study 2's definition | 36 | 4 — BLZ, FTM, MATIC, WAVES |

### 2.2 This is a five-to-nine-name strategy

`R_core` admits, as a mean per calendar year: **0.0** (2019), 0.3 (2020), 4.5 (2021), 5.8 (2022), 8.3
(2023), 7.2 (2024), 8.0 (2025), 8.9 (2026). Study 2's definition has a **median set size of 10 per
day (min 1, max 15)**, with 2 distinct names in 2020 rising to 19 in 2026.

**Before 2020-11 essentially nothing qualifies**, because the panel itself starts 2017-08. So the
"2019-22" regime column is, for every age-gated definition, really **2020-11 to 2022-12**. The
strictest definition (`R_tight`: age ≥ 4y, top-5, one survived cycle) is a **one-to-five-name**
strategy.

### 2.3 The age bar is the entire defence against LUNA, and it buys avoidance, not expectancy

Terra LUNA qualified under `L5` (2022-01-29 → 2022-05-13), `L10` (from 2021-10-20), `L20` (from
2021-03-17), `C1` (from 2021-08-16) and `R_noage` (from 2021-10-20). **It qualified under no
definition carrying age ≥ 3y**, because it listed 2020-08-21 and died at roughly 630 days old. Under
`L10` with a −50% ATH trigger the LUNA event realises **−99.9997% of the deployed tranche**, entered
2022-05-10 and closed three days later.

| definition family | median max drawdown | median worst single event |
|---|---|---|
| `R_tight` (strictest) | **−16.5%** | **−34.2%** |
| `R_core` | −32.6% | −49.4% |
| `L10` (liquidity only) | −65.7% | −85.9% |
| `A3` (age only) | −78.8% | −83.3% |
| `C1` (one survived cycle only) | **−83.2%** | **−90.5%** |

Monotone, in the direction the owner's correction predicts. **What the reliability definition buys
is the avoidance of one −100%. It does not buy a positive expectancy**, and §3 is why.

### 2.4 "Has survived a cycle" is worth 1.5pp — the seductive part of the story needs hindsight

Study 2's survivorship control is the cleanest measurement in either study. Same age, same liquidity,
same rank, same −50% dip depth, weekly sampling, delisted names marked out at their last print:

| condition | n | med 90d | med 180d | med 365d | hit 365d |
|---|---|---|---|---|---|
| has already survived-and-recovered a 50% drawdown | 1,160 | +3.1% | +4.7% | **+3.0%** | 52.2% |
| passes age + liquidity + top-20, has **not** | 1,186 | +3.1% | +4.2% | **+1.5%** | 51.4% |
| wide universe (1y old, $5M), same −50% depth | 14,679 | −15.4% | −28.4% | **−47.6%** | 24.6% |

Study 1 reaches the same verdict from the portfolio side: across the 6,720-config grid, the
**no-cycle-requirement** definition `R_nocyc` has a *higher* median Sharpe (**0.195**) than `R_core`
(**0.161**) and a higher median CAGR. Having survived a cycle contributes nothing that age,
liquidity and size do not already contribute.

**So the fifty-point swing the owner's instinct is pointing at is real** — the reliability filter
moves median forward 365-day return from −47.6% to +3.0% — **and it is almost entirely "old, liquid
and large", not "battle-tested".** The powerful version of the survivor story requires hindsight,
which means it does not work.

---

## 3. The dip hypothesis, tested honestly — and where it breaks

### 3.1 The dip-depth gradient is real and monotone. FORWARD.

On the point-in-time reliable set, weekly sampling, delisted names marked out at their last print:

| condition | n | med 90d | med 180d | med 365d | hit 365d | mean 365d |
|---|---|---|---|---|---|---|
| all reliable coin-weeks | 2,967 | −0.4% | +0.7% | −2.8% | 48.3% | +23.7% |
| dd365 ≤ −20% | 2,280 | +0.6% | +0.4% | −3.7% | 47.5% | +16.3% |
| dd365 ≤ −30% | 1,941 | +1.2% | +2.1% | −3.5% | 47.8% | +14.5% |
| dd365 ≤ −40% | 1,563 | +1.3% | +3.1% | −2.2% | 48.8% | +11.6% |
| dd365 ≤ −50% | 1,160 | +3.1% | +4.7% | +3.0% | 52.2% | +12.3% |
| dd365 ≤ −60% | 729 | +5.2% | +7.7% | +9.3% | 58.2% | +18.1% |
| dd365 ≤ −70% | 339 | +5.9% | +9.9% | **+17.1%** | **65.2%** | +22.2% |

Monotone in depth, over months, in exactly the direction the hypothesis predicts. **This is the part
of the owner's correction that the data confirms, and it is why the hypothesis deserved a fair test
where the ten-day universe-wide version did not.**

Study 1 confirms the direction independently on the portfolio side: median Sharpe **0.112** at a
−70% trigger against **0.031** at −30%. The effect is real. It is also far too small to change a
decision, and the next three subsections are why.

### 3.2 The recovery fails on a majority of events, even for the most reliable names

`R_core`, −50% from the running ATH, exit **only** on recovery to the prior ATH, 1,095-day cap:
**20 events, and 13 of them (65%) never recovered** — they hit the cap, the series ended, or they are
still open. The median +27.1% over a median 437 days is carried entirely by the 7 that did recover.
Worst individual outcomes: MATIC **−72.9%** (recovery attempt ended when the ticker stopped, 566
days), DOGE **−70.7%** (still open at 643 days), LTC **−61.0%** (1,095-day cap), XRP −33.5% (cap),
ADA −14.1% (cap).

Fixed 365-day holds **lose money at the median**, per deployed tranche, costed: `R_core` **−23.0%**
(19 events), `R_wide` **−33.3%** (29), `R_soft` **−44.7%** (47). Fixed 180-day holds are roughly flat
(`R_core` +6.1%).

Study 2's version of the same fact is the harder one to argue with. The **31 series that were
point-in-time reliable and then fell 50%** — every one of them, at its trigger date, at least three
years old, top-20 by liquidity, above $10M of daily volume, and having *already* recovered from a
≥50% drawdown once before:

> **median forward 365-day return −43.5%** (mean −3.2%). 21 of 31 eventually printed a new 365-day
> high, median **645 days** later — and a new 365-day high after 645 days is usually far *below* the
> entry price. ADA −54.1% at one year and **−78.8% today**; VET −65.3% / **−87.7%**; CHZ −78.5% /
> **−94.1%**; ATOM −43.5% / **−91.9%**; RUNE −77.0% / **−87.6%**; LINK −73.4% / −51.1%; FIL −56.4% /
> −83.4%; AVAX −12.2% / −62.5%. Against SOL **+640.6%** at one year and ZEC **+335.8%**.

**"BTC has drawn down 70-80% several times and made new highs each time" is true of BTC. It is not
true of the assets that pass a point-in-time reliability screen.** They looked exactly like BTC looks
now, by every rule available at the time.

### 3.3 The matched test kills the hypothesis before cash drag is even mentioned

For each of 128 cells (8 reliability definitions × 4 triggers × 4 exits), every dip trade was compared
to **simply holding BTC over its own identical entry-to-exit dates**, both costed. This removes cash
drag from the comparison entirely.

| | result |
|---|---|
| cells where the **median per-event excess** over holding BTC was ≤ 0 | **125 of 128** (strictly negative in 124) |
| cells where fewer than half the events beat BTC over their own dates | **125 of 128** |
| median share of events beating BTC | **29.5%** (min 13.5%, max 66.7%) |
| cells with a >50% win rate | **3** — all `R_tight`, 8–13 events, median excess +0.0 to +2.5pp |

Representative: `R_core` / −70% / recover-to-ATH — median dip **−22.6%** against median BTC
**+90.2%** over the same 832 days, 29.4% of events winning. `R_wide` / −70% / recover-to-ATH —
**−35.4%** against **+81.6%**, 17.9% winning.

**The months-long recovery is real, and it is BTC's recovery. Buying the drawdown in a reliable alt
is a worse way to own it.** The edge is not diluted by cash drag; there is no edge to dilute.

### 3.4 The full grid: 6,720 costed, total-capital configurations

Trigger: drawdown crossing −30/−50/−60/−70%, from the running ATH and separately from the trailing
1-year high. Entry: 1/2/3/4 tranches, a tranche per further −5/−10/−15%. Exit: fixed 30/90/180/365d,
recovery to the pre-event ATH, a new high, and trend re-established (close > MA125, min hold 30d), all
capped at 1,095 days, forced out at the last print if the series dies. Budget slots M = 5 and 10.

| test | result |
|---|---|
| beat BTC buy-and-hold on **CAGR** (49.23%) | **0 of 6,720 (0.00%)** |
| beat BTC buy-and-hold on **Calmar** (0.64) | 43 (0.64%) |
| beat BTC buy-and-hold on **Sharpe** (0.64) | 91 (1.35%) — **and 67 of those 91 rest on 3 to 6 events** |
| beat the BTC MA125 trend filter on CAGR / Calmar / Sharpe | **0 / 0 / 0** |
| beat `crisis-policy.md`'s 8-name trend book on CAGR (35.68%) | **2** — both `C1`, i.e. the LUNA-admitting definition, at −75.5% and −72.9% max drawdown, and **−39.0% / −38.9% in 2025-26** |
| median config | CAGR **+1.4%**, Sharpe **0.081** |
| configs that lose money outright | **35.6%** |
| best Sharpe anywhere in the family | **0.823** — on **4 events** |
| best CAGR anywhere in the family | 37.66% — `C1`, −60%, 2 tranches, 365d, at −75.5% drawdown |

### 3.5 Tranching reduces risk and creates no return — and the honest accounting is what shows it

Both studies agree, and they disagree about the sign of the *second-order* effect, which is worth
recording.

**Study 1, across the grid:** going 1 → 4 tranches moves median Sharpe **0.031 → 0.114** and median
max drawdown **−64.3% → −41.6%**, while median time in market falls **36.7% → 18.1%**. Widening the
spacing −5% → −15% does the same (median drawdown −58.3% → −41.8%). **Median CAGR is unchanged at
about 1.5%.** Scaling in buys a shallower curve by deploying less capital.

**Study 2, on BTC specifically:** tranches are *worse* than a single entry. −20/−35/−50/−65 returns
**29.3% CAGR** against **42.9%** for one entry at −50%; −30/−40/−50/−60 returns 29.2%. The shallow
tranches deploy capital into something that keeps falling.

Both are right, and they independently reproduce `crisis-policy.md`'s verdict on the shipped
−15/−25/−35% ladder, which it found to be **the worst policy at every horizon**. **Do not arm that
ladder.**

### 3.6 Regime dependence is the whole result

Across 6,720 configs, share positive by regime: **2019-22 36.7%** (median −6%), **2023-24 88.8%**
(median +27%), **2025-26 36.7%** (median −6%). One regime out of three.

And on the event side, the regime in which the hypothesis should shine most — buying the deepest
drawdowns in crypto history during the 2022 bear — is the one where it loses. Reliable set, dd365 ≤
−50%, median forward 365-day return by regime: **2019-22 −13.2%** (hit 37.6%), 2023-24 +30.1%
(59.5%), 2025-26 +11.5% (61.9%).

### 3.7 The purest form — dip-buy BTC itself — is exactly as strong as sixty coin flips

Sixty BTC-only configurations, total capital, cash idle at 0%: 4 beat BTC buy-and-hold on CAGR, 24 on
Calmar, 30 on Sharpe. Best is **−60% trigger, lump, hold 365 days: +5,320% total, CAGR 67.6%, MaxDD
−50.6%, Calmar 1.34, Sharpe 1.162, 51.7% time in market — on FOUR trades.**

The expected best-of-60 Sharpe from sixty zero-skill trials at T = 7.73 years is **1.160**. The
observed best is **1.162**. *It sits on its own null to three decimal places.*

Start-date sensitivity confirms the diagnosis. The winning parameter set changes completely with the
start date — −60%/lump/365d at a 2019-01 start, −70%/4-tranches/recovery at 2019-07 and 2020-01,
−50%/lump/recovery at 2020-07 — and a 2019-01-01 start begins with BTC already **−80.1% below its
2017 ATH**, so every deep trigger fires on bar one. At a 2021-01-01 start **the best config has one
trade.** ETH behaves the same way: best Sharpe 0.783 on 4 trades.

Study 2's independent version: the BTC −50% rule is long from day one on 2019-01-01, and the one
construction that beat hold on both axes (the BTC dip signal held in equal-weight BTC/ETH/BNB, 49.8%
CAGR / Sharpe 1.32 / MaxDD −45.4%) **beats hold at exactly one of seven start dates**:

| start | hold | dip in BTC | dip in BTC/ETH/BNB |
|---|---|---|---|
| 2019-01-01 | 49.2% | 42.9% | **49.8%** |
| 2019-04-01 | 49.6% | 43.1% | 43.8% |
| 2019-07-01 | 32.7% | 30.8% | 32.4% |
| 2020-01-01 | 44.0% | 33.5% | 35.2% |
| 2020-07-01 | 42.7% | 23.2% | 25.1% |
| 2021-01-01 | 20.4% | 19.9% | 18.8% |
| 2022-01-01 | 13.4% | 7.3% | 6.6% |

And the third leg of that basket is hindsight: swapping it gives LINK 51.1%, LTC 47.4%, ADA 47.2%,
XRP 40.6%, SOL 38.0% — a 13pp range.

### 3.8 Out of sample: three anecdotes, reported as absence of validation

**Study 1, purged and embargoed walk-forward** (365d purge + 30d embargo, selection on train Calmar
over 120 configs, applied unchanged to test):

| fold | picked | OOS dip | OOS BTC | OOS trend | events |
|---|---|---|---|---|---|
| train ..2021-06, test 2022-07→2023-12 | `R_soft/−30/hi365/4 tranches/recovery` | **+32.8%** | +78.8% | +58.3% | **1** |
| train ..2022-12, test 2024-01→2025-03 | `R_soft/−30/hi365/3 tranches/recovery` | **−20.9%** | +92.2% | +72.1% | **3** |
| train ..2024-06, test 2025-07→2026-09 | `R_core/−50/hi365/3 tranches/180d` | **−1.6%** | −28.7% | −23.2% | **8** |

The unfitted a-priori config (`R_core/−50/ATH/4 tranches/180d`) returned **−3.7%, −2.8%, +6.0%** on 2,
3 and 5 events. **With 1, 3 and 8 test events this is not a walk-forward; it is three anecdotes, and
it is reported as absence of validation rather than as a result.**

**Study 2, purged and embargoed walk-forward on the BTC dip rule**, re-choosing the threshold every
year from all prior data minus a 365-day embargo — the choice was stable at −0.50/new-high in all five
years, 1,728 OOS days 2022-01-01 → 2026-09-24:

| | total | CAGR | vol | Sharpe | MaxDD | in market |
|---|---|---|---|---|---|---|
| rule | +39.7% | **7.3%** | 29.8% | 0.25 | −50.4% | 30% of days |
| hold | +81.7% | **13.4%** | 50.7% | 0.27 | −66.9% | 100% |

Per year: 2022 −45.1% vs −64.2%; 2023 +85.2% vs +155.6%; **2024 0.0% (in cash all year) vs +120.8%**;
2025 0.0% vs −6.3%; 2026 +54.3% vs −5.7%. It loses 6.1pp of CAGR, ties on Sharpe to two decimals, and
wins 16.5pp of drawdown.

### 3.9 Events per decade: this family cannot be validated inside the data that exists

`R_core` at −50% from ATH: **32 events per decade across 11 names**. `R_tight` at −70%: 5 to 26 per
decade across 4-7 names. **BTC alone at −50%: 16 distinct trigger crossings in 7.73 years, which
cluster into about five independent market episodes** — 2019-01, 2020-03, 2021-06/07, 2022-05
(extending through 2023-02) and 2026-06/07. Fifteen of the sixteen crossings were profitable, and the
largest of them — triggered 2022-05-09, 265 days below −50% — returned **+2.0% over 410 days**.

Five positive episodes is the argument for the hypothesis. Five is also the argument against believing
it.

---

## 4. Why BTC's return is so hard to improve on — the structural facts

These are descriptive arithmetic on 2,824 days with no model and no selection, and they bound
everything in §8.

### 4.1 The return is twenty days, and the losses are just as concentrated

2019-01-01 → 2026-09-24: 51.0% of days are up. Sum of up-day log returns **+31.742**; sum of down-day
log returns **−28.620**; net **3.121**. *The whole nine-year result is 5% of the motion.*

| days removed (set to 0%, i.e. in cash exactly then) | total | CAGR | MaxDD | share of total lost |
|---|---|---|---|---|
| none | +2,167.9% | 49.70% | −76.6% | — |
| best 1 | +1,797.2% | 46.28% | −76.6% | **17.1%** |
| best 5 | +942.0% | 35.38% | −79.6% | 56.5% |
| best 10 | +469.8% | 25.22% | −80.6% | 78.3% |
| best 20 | **+96.7%** | 9.14% | **−87.3%** | **95.5%** |
| best 50 | −87.1% | −23.29% | −96.4% | 104.0% |
| worst 20 | **+42,353%** | 118.60% | −53.6% | — |

The top 20 up days are **78%** of the net log return; the worst 20 days are **−2.930** log, *larger in
magnitude* than the best 20's +2.445. Removing 20 days makes the drawdown **worse** (−87.3%), because
what is left is the down tail.

### 4.2 And the best and worst days are the same days, in the same places

Median trailing 20-day annualised vol before a best day **0.59**, before a worst day **0.62**,
unconditional **0.51** — symmetric to two decimals. A **perfect oracle** that removes the best *and*
worst 20 days gains only **+0.485 of log return** (CAGR 49.7% → 59.4%) and turns **negative** at
k = 100. A real filter does worse: flat when trailing vol20 is in its own top 20% gives 40.3% CAGR
against 49.7% and barely improves drawdown (−72.9% vs −76.6%); at top-40% it collapses to 14.4% with a
**worse** drawdown (−77.1%).

Worse for trend rules specifically: **of BTC's 20 best days only 40% occurred above the 200-day MA**,
against 57.5% unconditionally, with a median drawdown-from-365d-high of **−37.3%**. The very best days
happen in deep drawdowns below the trend filter — structurally, exactly where a trend filter is in
cash. This is consistent with `crisis-policy.md`'s independent finding that the MA125 filter was
already out of the market at T0 in **11 of 22** crash events.

### 4.3 The zero-skill baseline, and how steep the payoff to real targeting is

5,000 draws, a rule that sits out x% of days at random (hold = 49.2% CAGR):

| out of market | median CAGR | 5th–95th pct | P(beat hold) |
|---|---|---|---|
| 20% | 37.7% | 19.0 – 59.1% | **18%** |
| 50% | 22.3% | 1.5 – 46.2% | 4% |
| 67% | 14.1% | −4.0 – 35.6% | **~0%** |

And of a 20%-out rule's 565 out-days, if just **5%** of them are the true worst days and the rest
random, CAGR goes 18.2% → **126.9%**; at 25% correctly targeted, **318%**. *That steepness is the
prize, and it is precisely why a backtest of a timing rule looks brilliant on noise.*

### 4.4 The short constraint costs nothing. The leverage constraint is what binds.

**Short selling is a gift, not a cost.** The same 15-variant trend ensemble, long-only versus
long/short, costs on:

| window | long-only | long/short |
|---|---|---|
| 2019-2026 | 41.3% / Sharpe 1.12 / −43.5% | **11.9% / 0.24 / −56.2%** |
| 2021-2026 | 16.1% / 0.56 / −39.0% | −7.7% / −0.16 / −70.6% |
| 2023-24 | 51.4% / 1.54 / −26.9% | −11.6% / −0.29 / −56.2% |
| 2025-26 | 3.4% / 0.21 / −21.7% | −1.1% / −0.03 / −34.4% |
| 2017-20 (never looked at) | 67.9% / 1.64 / −44.4% | 10.9% / 0.17 / −70.2% |

Worse in all six windows, on both axes. With a directional IC of about zero, a short leg has nothing
to trade on — it pays costs and fights the drift. Cross-sectionally the short leg is nearly redundant
too: long-only captures **91% of the mean and 90% of the median** of the loser-avoidance spread
(pass-minus-universe +7.20pp / +11.64pp against pass-minus-fail +7.88pp / +12.98pp at h = 90d,
n = 48,693 coin-weeks), and the missing tenth sits in names that are illiquid, high-vol and mostly
unshortable on spot anyway.

**Leverage is the binding constraint, and it is not worth having.** With BTC perp funding charged on
the borrowed notional (12.25%/yr per 1× over the full history; 17.83%/yr in 2019-22, 4.29%/yr in
2025-26; 14.2% of prints negative):

| book | CAGR | Sharpe | MaxDD | avg gross |
|---|---|---|---|---|
| BTC hold (spot, x1) | 49.2% | 0.80 | −76.6% | 1.00 |
| dip rule x1 | 42.9% | 1.22 | −50.4% | 0.33 |
| dip rule x2 | **77.8%** | 1.11 | **−80.9%** | 0.66 |
| dip rule x3 | 95.6% | 0.91 | **−94.5%** | 1.00 |

Leverage buys return by handing back the whole drawdown advantage. **Not recommended, and now
measured rather than asserted.**

### 4.5 The break-even accuracy curve, and where we actually sit

Balanced directional accuracy needed to **merely match** hold, derived non-parametrically from BTC's
own non-overlapping block returns, costs on:

| hold | p* no cost | p* with cost | trades/yr | cost bps/yr | required rank IC |
|---|---|---|---|---|---|
| 1d | 0.5257 | **0.5607** | 182.5 | 2,737 | 0.190 |
| 10d | 0.5769 | 0.5874 | 18.3 | 274 | 0.271 |
| 20d | 0.6000 | **0.6069** | 9.2 | 138 | **0.330** |
| 90d | 0.6755 | 0.6782 | 2.1 | 31 | 0.531 |
| 180d | 0.7352 | 0.7370 | 1.0 | 15 | 0.678 |

**Costs are almost irrelevant past ten-day holds** — 60.00% cost-free against 60.69% costed at
h = 20. This is the difference between this hypothesis and the 0.6%-target fast profile: the cost
floor is not what stops us. The curve *rises* with holding period because BTC's up-day log mass
(31.742) barely exceeds its down-day mass (28.620), so the longer the block, the larger the fraction
of the total return sitting in the blocks you might miss.

**Measured directional strength on BTC — 45 feature-horizon pairs, not one reaching |t| = 2:**

| feature | h=1 | h=5 | h=20 | h=60 | h=90 |
|---|---|---|---|---|---|
| `mom20` | 0.009 (t 0.5) | 0.014 (0.3) | 0.098 (1.2) | 0.068 (0.4) | 0.096 (0.5) |
| `mom90` | 0.030 (1.6) | 0.044 (1.0) | 0.065 (0.8) | 0.072 (0.5) | 0.041 (0.2) |
| `above200dMA` | 0.003 (0.2) | **−0.016** | **−0.051** | **−0.060** | **−0.037** |
| `dd365` | 0.010 (0.5) | 0.009 (0.2) | 0.019 (0.2) | 0.057 (0.4) | 0.087 (0.4) |
| `rsi14` | 0.021 (1.1) | 0.050 (1.2) | 0.109 (1.3) | 0.088 (0.6) | **0.123 (0.7)** |

Best measured: **IC 0.123 at t 0.7**. Required at that horizon: **0.531**. `above200dMA` has the
**wrong sign at every horizon beyond one day**. And the loser-avoidance IC of 0.13 maps to 54.15%
balanced accuracy — below break-even at *every* horizon, and it is a *which-coin* number, not a
*direction* number.

---

## 5. The measurement neither study ran — is a dip rule worth anything incremental to the trend filter we already own?

`crisis-policy.md` §2 named this gap explicitly for its own −25% tier: *"Nobody has run a −25%
partial tier incremental to the trend filter. That is the single measurement that would justify this
tier, and it does not exist yet."* The same gap applied to the dip rule. I ran it.

Construction: the 15-variant trend ensemble sets the position; when the ensemble is off **and** BTC is
≥50% below its 365-day high, the dip forces the position long anyway (`max(ensemble, dip)`), exiting
on a new 365-day high. One day of lag on everything, costs on.

### 5.1 In-sample it is spectacular, and it is a spike

| BTC, 2019-2026 | CAGR | Sharpe | MaxDD | avg gross |
|---|---|---|---|---|
| BTC hold | 49.7% | 0.81 | −76.6% | 1.00 |
| trend ensemble alone | 47.0% | 1.21 | **−43.5%** | 0.52 |
| **ensemble OR dip(−50%)** | **72.4%** | **1.49** | −64.1% | 0.71 |

It beats hold on return **and** drawdown **and** Sharpe, and it beats the free static BTC/cash mix at
equal drawdown by **+32.7pp** of CAGR. It also **survives extra signal lag better than the ensemble
does** (72.4 → 67.7 → 67.1 → 63.8 at 1/2/3/5 bars, against the ensemble's 47.0 → 45.0 → 41.6 → 35.8),
which is genuinely unusual and is the strongest single argument in its favour.

Then the entry level:

| dip entry added to the ensemble | CAGR | Sharpe | MaxDD | ΔCAGR vs ensemble alone |
|---|---|---|---|---|
| −0.20 | 50.4% | 0.83 | −76.1% | +3.3pp |
| −0.30 | 45.0% | 0.79 | −74.3% | **−2.1pp** |
| −0.40 | 56.1% | 1.00 | −67.9% | +9.1pp |
| **−0.50** | **72.4%** | **1.49** | −64.1% | **+25.4pp** |
| −0.60 | 61.3% | 1.33 | −62.6% | +14.3pp |
| −0.70 | 55.3% | 1.28 | −47.6% | +8.3pp |

Non-monotone, with a single spike whose immediate neighbours are 11 to 16 points worse. **The repo's
own doctrine — pick the middle of the plateau, never the peak, from the MA125 choice and reaffirmed by
`growth-audit.md` §1.5 — forbids this.**

### 5.2 And then the window nobody looked at destroys it

Both studies start in 2019. The panel starts 2017-08. The 2018 bear market is right there.

| 2017-08-17 → 2018-12-31 | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| BTC hold | −10.2% | −0.11 | −83.2% |
| trend ensemble | **+3.9%** | **+0.13** | **−43.0%** |
| dip −50% alone | −48.6% | −0.78 | −72.1% |
| **ensemble OR dip(−50%)** | **−37.2%** | **−0.55** | **−75.3%** |

2018 alone: ensemble **−29.8%** at −36.8% drawdown; the union **−64.9%** at −72.6%; hold −73.0%. The
mechanism is not subtle — **the −50% override was long for 334 of 365 days in 2018 and held all the
way down.** That is `crisis-policy.md`'s "spends the reserve on something that keeps falling", in its
purest form, on the deepest bear in the panel.

Override days per calendar year: 2018 **334**, 2019 95, 2020 137, 2021 107, 2022 237, 2023 111, 2024
**0**, 2025 **0**, 2026 108. In 2019-2026 the trigger happened to fire near bottoms. In 2018 it fired
near the top of the descent. **That is the definition of a sample artifact, and it is invisible if you
start the backtest in 2019.**

### 5.3 The one pre-specified fix fails

The failure mode is "arms while still falling", so the fix is a confirmation: the dip may arm only
once the fall has stopped (close > own MA50). Tested at the a-priori −50% level only, no sweep.

| book | full panel CAGR | Sharpe | MaxDD | 2018 CAGR | 2019-2026 CAGR |
|---|---|---|---|---|---|
| trend ensemble | 39.5% | **1.05** | **−44.5%** | −29.8% | 47.0% |
| ensemble OR dip(−50%) | **48.0%** | 0.92 | −75.3% | −64.9% | **72.4%** |
| ensemble OR dip, confirmed > MA50 | 31.9% | 0.71 | −76.3% | **−66.3%** | 51.3% |

**It does not fix 2018 (−66.3% against the unconfirmed −64.9%) and it destroys most of the gain
elsewhere.** The confirmation cuts override days in 2018 only from 334 to 256, and removes the 2020,
2021 and 2026 overrides entirely — i.e. it filters out the ones that worked and keeps the one that
did not. Recorded so nobody re-proposes it.

### 5.4 Out of sample, incremental, the dip adds nothing

Purged and embargoed walk-forward, entry level re-chosen every year from all prior data minus a
365-day embargo, scored on train Calmar over the 6-point grid, 2,459 OOS days 2020-01-01 → 2026-09-24:

| | total | CAGR | Sharpe | MaxDD |
|---|---|---|---|---|
| union (entry re-chosen yearly) | +1,167.2% | **45.78%** | 0.97 | **−62.59%** |
| trend ensemble (nothing selected) | +907.5% | 40.90% | **1.12** | **−43.54%** |
| BTC hold | +1,067.1% | 44.01% | 0.73 | −76.63% |

Per year the union is **identical to the ensemble in 4 of 7 OOS years** — the trigger adds nothing at
all. The entire difference is winning 2020 (+276.4% vs +182.7%) and 2023 (+138.1% vs +75.0%) and
losing 2022 (**−43.2% vs −18.3%**). The annual pick was unstable: −0.20, −0.70, −0.60, −0.50, −0.60,
−0.60, −0.60.

And the procedure is not stable in itself. Re-running the same walk-forward with the confirmed variant
scoring the training window changed every pick to −0.70 and moved the aggregate from **45.78% /
Sharpe 0.97 / −62.6%** to **46.89% / 1.15 / −47.6%** — 0.18 of Sharpe and 15 points of drawdown, from
a change that should not have mattered. **That is a search-noise signature, and it is the reason this
section ends in a refusal rather than a recommendation.**

### 5.5 Verdict on the dip overlay

**Do not ship it.** It is the first thing in this project to beat hold on all three axes in-sample and
in a purged walk-forward, and:

* it buys that by **more than doubling the drawdown the mandate exists to avoid** (−62.6% OOS against
  the ensemble's −43.5%), for **+4.9pp of CAGR**;
* it **fails catastrophically in the one bear market outside the test window**;
* its threshold is a **spike**, its walk-forward picks are **unstable**, and its aggregate moves 0.18
  of Sharpe on an irrelevant procedural change;
* its in-sample Sharpe of 1.49 is nowhere near the deflated hurdle of **2.42** (§6);
* it is **28,678 years** from being statistically distinguishable from the ensemble alone.

The honest one-line summary: *the dip overlay converts the ensemble's drawdown advantage into return,
and the mandate says that is the wrong direction.*

---

## 6. The statistical bar, stated before anything is recommended

### 6.1 Trials, counted

| source | selection trials | screens |
|---|---|---|
| `growth-audit.md` (all four audits) | ~177 | — |
| Study 1 (reliable-coin dip family) | **7,140** — 6,720 portfolio grid + 60 BTC/ETH + 240 non-primary start dates + 120 walk-forward family | 560 event-study cells, 128 matched cells, 14 definitions, 90 pre-fix configs |
| Study 2 (what it would take to beat hold) | **96** | ~120 |
| This document (§1.4, §5) | **~30** — 10 books + 6-point entry grid + 6-point walk-forward family + confirmed variant + 3 BTC/ETH variants | ~120 (lag, window, static-mix and turnover diagnostics) |
| **cumulative** | **≈ 7,443** | ≈ 930 |

### 6.2 The hurdle, from the repo's own estimator

`runs/features/sampling.py: expected_max_sharpe` — the Bailey–López de Prado approximation, verified
against its pinned self-test values (0.862 at N=10, 1.050 at 50, 1.187 at 200, 1.329 at 1000, T=9.1):

| N | E[max Sharpe] T=9.1 | T=7.73 |
|---|---|---|
| 30 | 0.994 | 1.079 |
| 273 | 1.216 | 1.319 |
| 7,443 | **1.486** | **1.612** |

| candidate | its Sharpe | baseline | hurdle | clears? |
|---|---|---|---|---|
| best in the reliable-coin dip family | 0.823 (4 events) | BTC 0.64 | **2.252** | **no** |
| best BTC-only dip config | 1.162 (4 trades) | BTC 0.64 | 2.252 | **no** — and it sits on its own best-of-60 null of 1.160 |
| dip signal in BTC/ETH/BNB | 1.32 (hindsight basket) | BTC 0.80 | 2.422 | **no** |
| ensemble OR dip, in-sample | 1.49 | BTC 0.81 | 2.422 | **no** |
| **BTC/ETH trend ensemble, full panel** | **1.08** | BTC 0.58 | **2.066** | **no** |

**Nothing in this document clears its deflated hurdle. Including the thing §8 recommends.** Say it
in the recommendation, not in a footnote.

### 6.3 Power: what a live period can and cannot settle

`years_to_detect` at 80% power, two-sided α = 0.05:

| comparison | years |
|---|---|
| BTC/ETH ensemble 1.08 vs hold 0.58 (full panel) | **86** |
| BTC/ETH ensemble 1.19 vs hold 0.81 (2019-2026) | 165 |
| union in-sample 1.49 vs hold 0.81 | 58 |
| dip walk-forward 0.25 vs hold 0.27 | 40,573 |
| union OOS 1.15 vs ensemble OOS 1.12 | **28,678** |
| BTC/ETH ensemble 1.08 vs BTC-only ensemble 1.05 | 27,336 |

`modes.live.min_test_days` proves the **plumbing** and nothing whatsoever about edge. A good quarter is
not validation; the number is 86 years.

### 6.4 One convention clash for a human to settle

The repo contains **two incompatible hurdle conventions**: `edge-audit`'s `SKILL.md` and
`references/method.md` §3 say the hurdle is `baseline + expected_max_sharpe(N, T)`, while
`growth-audit.md` §2.1 compared a candidate Sharpe against `expected_max_sharpe` **alone** (1.25
against ~1.10-1.19). This document uses the skill's convention throughout, because the skill owns the
definition. **Settle it in `edge-audit/references/method.md` rather than re-litigating it per
report** — under the looser reading, the trend ensemble's growth-audit Sharpe of 1.25 clears by 0.06;
under the skill's own rule nothing in nine years of this repo's history has come within 0.7.

### 6.5 The trial counter was not written

Nothing here touched `knowledge/state/trial_counter.json` — it is live state the running system reads.
The file does not currently exist on this host. **Before any number in this document is quoted at a
change gate, ≈7,443 selection trials and ≈930 screens must be added through
`audit_stats.py trials --add`**, and the per-event statistics need an effective-N before they are
reportable at all. Under the skill's own rule the counter only ever grows, so omitting them would
leave the next report's hurdle understated.

---

## 7. Honest limits

1. **The panel starts 2017-08-17, so BTC's own age can only be measured from there.** Any age ≥ 3y
   gate is empty until 2020-08 and age ≥ 4y until 2021-08. BTC's 2013-2015 and 2018 cycles are
   outside or barely inside the data — **the very drawdown-and-recovery history that motivates the
   hypothesis is largely unobservable here.** A longer panel could change the reliability rosters,
   though it would have to change them enormously to reverse a 0-of-6,720 result.

2. **Effective sample size is the dominant limit and no method fixes it.** Five to nine names qualify
   at a time, one to five under the strict definition; the best-Sharpe configs carry 4-5 events; BTC
   alone at −50% fires about five independent times a decade. Events overlap heavily in calendar time
   — every name draws down in the same months — so the effective number of independent observations is
   far below the event count. **No effective-N was computed on these events, and `edge-audit` would be
   right to refuse any per-event statistic here for exactly that reason.** Treat every event-level
   median as directional, not as a test.

3. **Two numbers from the studies' own reports do not reproduce, and are not used here.** (a) Study
   2's report quotes a 1.5× levered trend ensemble at "54.4% CAGR / −60.9% MaxDD with funding charged"
   as the one construction beating hold on both axes; the surviving `p3_leverage.py` charges no funding
   on its leverage rows and prints x2 = 74.5% / −72.2%, and `p12.py`, which does charge funding, covers
   the dip rule rather than the ensemble. **The claim is withdrawn**; §4.4 uses the reproducible
   funded numbers instead, which do *not* beat hold on both axes. (b) Study 1's report states the
   matched test lost "in 125 of 128 cells" comparing medians; the median-of-medians comparison is
   **117 of 128** and the *paired* per-event statistic is 125 of 128. §3.3 uses the paired one and
   says which it is.

4. **Study 2's trend-ensemble return was understated by 5.8pp of CAGR** by a signal warm-up inside the
   measurement window (§1.4). This was found by re-deriving rather than transcribing. It is a warning
   about the whole class: **any book whose signal has a lookback longer than a few weeks must be
   measured on a panel that starts earlier than the measurement window**, and there is no assertion in
   the repo that enforces it. Build item 7.

5. **The 2019-01-01 start hands every deep trigger a free entry on bar one**, with BTC at −80.1% from
   its ATH. Measured and reported rather than hidden (§3.7), but it means every 2019-start dip number
   is contaminated by where the sample happens to begin. The 2021-01-01 start, which is clean, gives
   the best config **one trade**.

6. **The §5 overlay's in-sample result is the strongest thing measured and the least trustworthy.** It
   rests on about five independent BTC episodes, its threshold is a spike, and its walk-forward moved
   0.18 of Sharpe on a procedural change that should not have mattered. I did not run it at higher fold
   counts because shorter test windows contain zero events.

7. **Delisting is marked at the last available close, which is optimistic** — it assumes you sold at
   the final print rather than being frozen in a halted book. Optimistic in the direction of making
   the dip strategies look *better* than they were. `growth-audit.md` found its own filter's advantage
   *widens* under the harsher −100% convention; that re-run was not done for the dip results here.

8. **Cash earns 0% throughout**, which is generous to buy-and-hold by roughly 2-4pp/yr of forgone
   T-bill yield in 2023-2026, and therefore **understates the cash-heavy books by about 1-3pp/yr**.
   Uncorrected, and it biases against the recommendation rather than for it.

9. **No market-impact model beyond the flat 15 bps per side.** Liquidity is trailing-90d median quote
   volume with a flat floor. For a top-5 or top-10 name at $20,000 that is conservative enough, but a
   five-name book concentrating into a crashing asset is exactly where slippage misbehaves, and a
   dip that is being bought *during* a crash will realise worse than 5 bps of slippage. No stress cost
   was modelled.

10. **The BTC/ETH pair is not my selection — it is `universe.core` — but the 50/50 weighting is.**
    ETH helps on some windows (2021-2026: 33.9% vs BTC-only 24.8%; 2025-26: 11.0% vs 2.0%) and hurts
    on others (2023-24: 53.7% vs 73.9%; 2019: +51.7% vs +96.0%). The mixed sign is evidence it is
    diversification rather than a hindsight pick, and the two books are **27,336 years** from being
    distinguishable. Do not read +3.6pp of full-panel CAGR as a result.

11. **Funding history exists in the runtime and was used only to price leverage.** This is a spot-only
    long-only question; funding would have entered only as a sentiment conditioner, and
    `growth-audit.md` §1.6 already rejects it as an entry filter.

12. **The hybrid nobody tested is "BTC core with dip tranches funded from the core rather than from
    idle cash".** Study 2 proved the gross-capped version is byte-identical to buy-and-hold, and §3.3
    says the dip leg would subtract from it. Not re-run.

13. **Nothing here ran through Freqtrade or the risk gate.** These are vectorised daily close-to-close
    panels. No minimum notionals, no step sizes, none of the gate's checks, no limit-fill risk, no
    monthly fee budget, no intrabar path, no stops. **Any number would move once real execution
    applies, and the direction of that move is down.** At the seed ceiling in `trading.max_seed_usdt`
    a satellite trim is already below `risk.min_notional_usdt` — `crisis-policy.md` §2 Tier 2 has the
    arithmetic, and the same constraint applies to any tranche ladder proposed here.

14. **Three regimes is three.** The boundaries were given in advance rather than discovered — the
    right choice — but 2019-22 / 2023-24 / 2025-26 is one market's one nine-year history containing at
    most three genuine cycles. **The 2023-24 result, where every rule loses 84 to 118 points of CAGR
    to buy-and-hold, is the specific scenario that should be assumed to recur**, because a clean BTC
    bull is the modal way this project's whole approach fails.

15. **The claim not being made:** nothing here shows the dip idea *loses money* forward. It shows it
    does not beat buy-and-hold on return out of sample, that its threshold is a search peak, that it
    depends on one start date, and that it does not come within 0.9 of Sharpe of its own hurdle. Those
    are reasons not to ship it, not a prediction that it fails. Anyone reading this as *"the dip idea
    is wrong"* has read it as strongly as anyone reading it as *"the dip idea works"*. On five
    episodes, this question cannot be settled by this data, and §6.3 says live trading will not settle
    it either.

---

## 8. The recommended configuration to run next

### 8.1 What to run

**Core-only, two assets, trend-ensemble gated, no dip sleeve, no crash-reactive selling.** In the
system's own knobs — values live in `config/earn.yaml` and the sleeve params, and are not restated
here:

| # | change | tier | why |
|---|---|---|---|
| 1 | **Replace the single MA gate with the 15-variant trend ensemble** on both `universe.core` assets. Signals averaged into **one** book traded once — not fifteen books. | 2 (`strategies/**`) | §0.3, §0.4. This is `growth-audit.md` §2.1 RANK 1, now supported on a longer window and with the §1.4 artifact removed |
| 2 | **Concentrate: satellites to zero or to the floor.** Reduce `risk.max_satellite_gross` and `risk.max_satellite_positions`, and apply the `growth-audit.md` §1.5 exclusion filter to tier membership so the 7 failing pairs cannot be held. | 2 (`config/**`) | §3.4 (0 of 6,720), §3.2 (median −43.5%), study 2's reliable-set book at **−6.4% to −8.1% CAGR / −84.6% drawdown**, `crisis-policy.md`'s 22-of-22 core recovery vs 65.1% of alts still underwater |
| 3 | **Fix the daily stop:** halve instead of flatten, or widen the trigger. | 2 (`config/**`) | `crisis-policy.md` §0: the shipped shape is the **worst policy measured** (−5.2% CAGR, 66.7 exits/yr, 10.66%/yr in fees); halving at the same trigger gives **+13.05%** |
| 4 | **Ship the profit ladder** (`execution.take_profit`, within its declared bounds). | 2 then 1 | `growth-audit.md`: **77.5%** of +100% run-ups were mostly or entirely given back within 60 days, and today there is no take-profit level on any position |
| 5 | **Do not arm any dip ladder, in any form.** Not −15/−25/−35, not −50 tranches, not the §5 overlay. | — | §3.5, §5.2, §5.5, and `crisis-policy.md`'s independent verdict on the shipped ladder |

The dip work is not wasted: it produces **one** shippable rule, and it is a negative one —
`crisis-policy.md`'s Tier 1, **stop buying** inside a bounded window, which touches no held position
and costs zero in fees. Buying *more* into a deep drawdown is what §5.2 measured at −64.9% in 2018.

### 8.2 Expected return, and the evidence behind the number

The recommended book, BTC/ETH trend ensemble, measured on the full panel and every sub-window, costs
on. **There is no single expected return, and quoting one would be the dishonest part.** The rolling
distribution is the answer.

**Rolling 365-day total return, 2019-2026 (2,824 overlapping windows):**

| | min | p10 | p25 | median | p75 | p90 | max | share < 0 |
|---|---|---|---|---|---|---|---|---|
| BTC hold | −77.8% | −46.3% | −21.7% | **+41.2%** | +125.4% | +265.1% | +1,092% | **35%** |
| BTC trend ensemble | −38.9% | −18.5% | −5.1% | +30.5% | +94.2% | +231.9% | +504% | 30% |
| **BTC/ETH trend ensemble** | **−38.9%** | **−15.0%** | **+2.2%** | **+24.8%** | +66.1% | +317.0% | +869% | **22%** |

**So the honest expectation for a year in this book is: median +25%, a quarter of years under +2%,
one year in five below −15%, worst observed −39%, and roughly one year in five where it doubles.** A
year in BTC is median +41% with one year in three negative and a worst of −78%.

**Calendar years, costs on, for the same numbers without percentile smoothing:**

| year | BTC hold | BTC ensemble | BTC/ETH ensemble |
|---|---|---|---|
| 2017 (from 08-17) | **+220.1%** | +50.2% | +47.0% |
| 2018 | −73.0% | −29.8% | **−20.1%** |
| 2019 | +94.3% | **+96.0%** | +51.7% |
| 2020 | **+302.0%** | +182.7% | +175.6% |
| 2021 | +59.8% | +39.2% | **+136.5%** |
| 2022 | −64.2% | **−18.3%** | −20.6% |
| 2023 | **+155.6%** | +75.0% | +55.5% |
| 2024 | **+121.3%** | +73.0% | +52.1% |
| 2025 | −6.3% | −5.0% | **+7.5%** |
| 2026 (to 09-24) | −4.2% | +8.8% | **+11.5%** |

Second-best in five years out of ten, best in four, and it never posts the worst number. **It loses
every clean bull and wins every bear and every chop.**

**And the number to plan on for the next twelve months, stated separately, because the regime is
what it is:** BTC is 32.6% below its own 365-day high as of 2026-09-24, and in the last twelve months
BTC hold returned **−25.9%** while this book returned **−0.9%**. In 2025-26 it returned **+11.0%
against BTC's −6.1%**. If the chop continues, expect single digits. **If a clean bull starts, expect
to be beaten badly by doing nothing, and the honest thing is to have said so in advance.**

### 8.3 Expected drawdown, and the evidence behind the number

| measure | BTC hold | BTC/ETH trend ensemble |
|---|---|---|
| worst drawdown, full panel (9.11y) | **−83.2%** | **−45.6%** |
| worst drawdown 2019-2026 | −76.6% | −45.6% |
| worst drawdown 2025-26 | −53.0% | −26.0% |
| worst 30-day return | −53.0% | −35.5% |
| p10 of 30-day returns | −17.5% | −7.9% |
| share of 30-day windows worse than −10% | **20%** | **7%** |
| share of 30-day windows negative | 43% | **54%** |
| worst rolling 12 months | −77.8% | −38.9% |

**Plan for a −45% drawdown and be prepared for worse.** −45.6% is the worst observed over 9.11 years
including the 2018 and 2022 bears; the true forward worst case is unknown and higher. On $20,000 that
is an account showing about $10,900 at the bottom, against BTC's $3,400.

Note the one row that goes the wrong way: **this book is negative in 54% of 30-day windows against
hold's 43%.** It spends more months doing nothing, and that is exactly the sensation the owner is
reporting. It is not the system failing; it is what a cash-heavy book feels like.

### 8.4 Cost, capacity and the fee budget

One-way turnover **8.62× NAV per year**, which at 15 bps/side is a fee drag of **1.29%/yr** — about a
tenth of the monthly fee budget in `risk.max_fee_pct_per_month`. For contrast, the shipped daily stop
spends **10.66%/yr**, i.e. 89% of that budget, on stop-outs alone (`crisis-policy.md` §0). Turnover
by year ranges 4.9× (2026 partial) to 10.7× (2024). This is comfortably inside every declared limit,
and it is the one operational fact that makes item 1 in §8.1 cheap.

### 8.5 The state of the book today

As of 2026-09-24: BTC $83,978, **32.6% below its 365-day high**, and the ensemble is at **full weight
on both BTC and ETH**. So the recommendation is not "sit in cash" — the recommended book is fully
invested right now, and the last twelve months are the period in which it was mostly not.

### 8.6 What §8 is *not* claiming

* It does **not** clear the deflated hurdle (Sharpe 1.08 against a required 2.07, §6.2).
* It is **86 years** from being statistically distinguishable from buy-and-hold (§6.3).
* It is **27,336 years** from being distinguishable from the BTC-only version of itself.
* Its full-panel win is **+4.5pp of CAGR**, which is inside any reasonable noise band. **The
  drawdown result (−45.6% against −83.2%) is the finding; the return result is a bonus that should
  not be relied on.**
* It gives up **84 points of CAGR** in a clean bull.

**The defensible claim is narrow and it is this:** the trend ensemble is worth **+22.1pp of CAGR over
a free static BTC/cash mix at the same drawdown** (47.0% against 25.0% at w = 0.43) — and a static mix
has no parameter, no trial count and no decay risk, so beating it is the real test of whether a rule
earns its complexity. `MA200` **fails** that test (−15.2pp) and the volatility filter **fails** it
(−6.4pp). The ensemble and the dip rule pass it, at +22.1pp and +13.9pp. **That is the honest size of
the achievable prize, and it is a drawdown prize.**

---

## 9. Build list, in priority order

Tier per `CLAUDE.md`. Nothing here is switched on by this document.

1. **The trend ensemble replaces the single MA gate, on BTC and ETH.** Tier 2 (`strategies/**`).
   Highest measured value per unit of risk in this document and in `growth-audit.md`. The single MA
   lookback is movable inside its declared bounds through `config/params-sleeve-a.json` (tier 1), but
   **do not** use that to move it to 125: §4 of `crypto-research.md`'s doctrine and this document's
   own MA sweep show a **44-point CAGR range across one integer of lookback** (MA50 63.4%, MA125
   59.4%, MA200 24.4%, MA225 19.5%), and MA125 degrades 59.4 → 52.7 → 39.9 → 30.5 under one to five
   bars of extra lag. **The ensemble exists precisely so that integer never has to be chosen.**
2. **Concentrate the book.** Tier 2 (`config/**`). Satellites to the floor; the `growth-audit.md`
   §1.5 exclusion filter as tier membership so the 7 failing pairs cannot be held.
3. **Fix `risk.daily_loss_stop` — halve, do not flatten.** Tier 2 (`config/**`), with the
   walk-forward attached. `crisis-policy.md` has the measurement; it is the single largest measured
   value destruction currently shipped.
4. **The profit ladder.** Tier 2 to build, tier 1 to tune inside `take_profit_pct` bounds.
5. **`crisis-policy.md` Tier 1 — `block_entries` with a bounded expiry.** Tier 2. Free, and the only
   crisis tier the evidence unambiguously supports. **It must never be able to cause an exit and never
   be able to stop one.**
6. **The split-guard test.** Tier 2 (`tests/**`). Assert the LUNAUSDT 2022-05-31 reuse is split and
   that the real −94% / −99.97% days of 2022-05-11/12 are **not**, on the last-valid-observation rule
   from §1.2. Today's inherited rule gets both backwards.
7. **A warm-up assertion in the backtest API.** Tier 2 (`evals/backtest_api.py`). Any signal whose
   longest lookback exceeds the leading data available before the measurement window must fail loudly,
   not silently read flat. §1.4 is what it costs: 5.8pp of CAGR and a wrong headline.
8. **Advance `knowledge/state/trial_counter.json`** by ≈7,443 selection trials and ≈930 screens
   before any figure here is quoted at a change gate. Tier 2 (`knowledge/state/**`, human only).
9. **Settle the hurdle convention** in `edge-audit/references/method.md` (§6.4). Tier 1.

### Dead ends, recorded so nobody rebuilds them

* **Dip-buying a basket of reliable coins, at any horizon or definition.** §3. 0 of 6,720.
* **Tranche ladders as a return device.** §3.5. They reduce risk by deploying less capital.
* **The dip overlay on top of the trend filter.** §5. Works 2019-2026, −64.9% in 2018.
* **The MA50-confirmed dip overlay.** §5.3. Filters out the episodes that worked.
* **Leverage, including modest leverage.** §4.4. Hands back the entire drawdown advantage, and costs
  a measured 12.25%/yr in funding.
* **A short leg.** §4.4. Worse on both axes in all six windows tested.
* **A volatility filter for drawdown control.** §4.2. The information at high volatility is
  directionally symmetric; a perfect oracle gains 9.7pp and real filters lose 9-35pp.
* **"Has survived multiple cycles" as a selection criterion.** §2.4. Worth 1.5pp point-in-time; the
  strong version needs hindsight.

---

## 10. What would tell us in 30 days that this is working — and what would tell us to stop

**First, the honest frame: 30 days cannot tell you about return.** The median 30-day return of the
recommended book is **−0.3%**, 54% of 30-day windows are negative, and §6.3 puts statistical
separation at 86 years. **Thirty days can only tell you whether the mechanism is behaving.** Anything
presented as a 30-day verdict on edge is a lie, including a good one.

### 10.1 Working — all five must hold

| # | check | threshold | where it comes from |
|---|---|---|---|
| 1 | **Exposure tracks the signal.** Realised average gross within ±0.10 of the ensemble's own computed weight, every day. | ±0.10 | The book's entire behaviour is its gross; §8.5 has it at 1.00 today |
| 2 | **Fee drag on budget.** Realised fees ≤ 0.15% of NAV over the 30 days. | 1.29%/yr ÷ 12 ≈ 0.11%; alarm at 0.15% | §8.4. This is the check that would have caught the shipped daily stop at 10.66%/yr |
| 3 | **Turnover in range.** 0.4 to 1.0× NAV one-way over the 30 days. | §8.4: 4.9-10.7×/yr | Above 1.0× means something is whipsawing; below 0.4× means a signal is stuck |
| 4 | **Drawdown stays inside its measured envelope.** No 30-day drawdown worse than −12%, against a measured p10 of −7.9% and a worst-ever of −35.5%. | −12% | §8.3. A −12% month is p3-ish; it is a *look at it* line, not a stop |
| 5 | **It loses less than BTC on down days.** On the 10 worst BTC days in the window, the book's mean loss ≤ 0.7× BTC's. | 0.7× | The mechanism's whole claim is absence during drawdowns (gross 0.45 average) |

If all five hold, **the correct conclusion is "the plumbing works", not "the strategy works"**, and
`crypto-research.md` §3 and `edge-audit` should be quoted saying so.

### 10.2 Stop — any one is sufficient

| # | trigger | threshold | why this number |
|---|---|---|---|
| 1 | **Fee drag off budget.** Realised fees > 0.5% of NAV in 30 days (i.e. > 6%/yr pace). | 0.5%/30d | Half the budget in `risk.max_fee_pct_per_month`. `crisis-policy.md` measured a shipped policy at 10.66%/yr; this is the tripwire that should have existed |
| 2 | **Turnover runaway.** > 2.0× NAV one-way in 30 days, or > 1.5× in any 10 days. | 2.0× | Twice the measured envelope. Indicates hysteresis failure or a data gap flapping the signal |
| 3 | **Drawdown beyond the envelope.** Any drawdown worse than **−50%**, or worse than **−25% while BTC's own drawdown is shallower than −15%**. | −50% / relative | −45.6% is the worst measured over 9.11 years. Losing more than BTC while BTC is calm means the book is the problem, not the market |
| 4 | **Wrong-way exposure.** Average gross above 0.80 across any 30 days in which BTC fell more than 20%. | 0.80 | The book exists to be absent in drawdowns. This is the §5.2 failure mode — 334 days long in 2018 — expressed as a live alarm |
| 5 | **Execution divergence.** Realised slippage above 5 bps/side for two consecutive weekly `tca` reports, or realised cost/round-trip above 0.45%. | 0.30% assumed, 0.45% alarm | The whole cost floor. §7 limit 13: every number moves down once real execution applies |
| 6 | **The signal is stale, not flat.** `knowledge/state/freshness.json` stale, or the per-asset state empty, for more than 24h. | 24h | `growth-audit.md`: `knowledge/earn.db` held **8 daily candles per pair** against the 201 required, so `assets: {}` and all three proposals ever produced abstained at 100% USDT. An empty signal is not a flat signal |
| 7 | **A quarterly decay failure.** Any shipped trend member at `|t| ≤ 1.5` for two consecutive quarters. | `edge-audit` rule | The retirement rule. **Note the specific fragility: the 15 members are worth about 1.23 independent bets (mean pairwise correlation 0.802). Averaging removes parameter-choice risk, not market risk. If trend following stops working, all fifteen fail together** |

### 10.3 What is explicitly *not* a stop signal

* **Losing to buy-and-hold BTC in a bull market.** §0.4 and §8.2 predict it: −84pp in 2023-24, −69pp
  in 2024, −126pp in 2020. This is the priced, disclosed cost of the configuration. Stopping because
  of it is buying the drawdown insurance and then cancelling it the month before the fire.
* **A negative month.** 54% of 30-day windows are negative by construction.
* **A good month.** §4.3: a zero-skill rule that sits out 20% of days has an 18% chance of beating
  hold over 7.7 years. A month proves nothing in either direction.

---

## Appendix — provenance

Everything was measured on this host on **2026-09-25**, and every headline number in this document
was **re-derived by running the study scripts**, not copied from their reports. That process produced
the corrections in §1.4 and §7 limit 3.

**Panel:** `~/earn-panels/panel_1d.parquet` (841,491 daily candles, 747 USDT tickers, 2017-08-17 →
2026-09-24, dead coins in), `wk.parquet`, `funding.parquet`.

**Study 1 — the reliable-coin dip family.** `~/dp1/`: `base.py` (panel, split guard, cycle counts),
`reliable.py` (14 point-in-time definitions and rosters), `core.py` (numpy event engine), `port.py`
(total-capital simulator), `pgrid.py` (6,720-config grid), `matched.py` (per-event vs BTC over
identical dates), `btconly.py`, `startsens.py`, `wfp.py`, `an.py`, `final.py`. Outputs re-read here:
`port_grid.parquet` (6,720 × 27), `matched.parquet` (128 × 12), `btconly.parquet` (120 × 15),
`startsens.parquet` (300 × 10), `wf.parquet` (3 × 22), `event_grid.parquet` (560 × 16).

**Study 2 — what it would take to beat hold.** `~/dp2/`: `p1_decompose.py`, `p1b_cluster.py`,
`p2_breakeven.py`, `p2b_where.py`, `p3_leverage.py`, `p4_xs.py`, `p5_dip.py`, `p6_port.py`, `p7.py`,
`p8.py`, `p9.py`, `p10.py`, `p11.py`, `p12.py`, `p13.py`, `p14.py`, derived panel `rel.parquet`.
**Re-run here and reproduced exactly:** p1, p2, p2b, p3, p5, p7, p8, p9, p10, p11, p12, p13, p14.

**This document's own work.** `~/dp3/`: `inc.py` (the incremental dip-vs-ensemble test, 10 books × 7
windows plus the entry grid), `inc2.py` (shift-invariance, the pre-2019 windows, the purged
walk-forward on the union, the static-mix comparison, override-day census), `inc3.py` (the
pre-specified MA50-confirmed variant and its walk-forward), `inc4.py` (turnover, rolling 365d/30d
distributions, calendar years, the $20,000 table, power), `xx.py` (the §1.4 warm-up isolation).
Copies of the sources are in this session's scratchpad.

**Cross-references:** `docs/design/growth-audit.md` (§0, §1.5, §2.1, §2.5, §5, §6),
`docs/design/crisis-policy.md` (§0, §2, §6), `docs/design/crypto-research.md` (§1.2, §3),
`docs/design/wide-universe.md`, `.claude/skills/edge-audit/references/method.md` (§1, §3, §3a, §4,
§5, §7), `runs/features/sampling.py: expected_max_sharpe`, `evals/backtest_api.py`,
`config/earn.yaml`.

**Not done:** no file in the momentum repo outside this one was written; no `ruff format`; no git
commit; no `.env` read; no `var/state/**` or `knowledge/state/**` write; no contact with the running
bots; no console call. No `earn-test` run applies, because no repo code changed — the studies are
measurement work living entirely in `~/dp1`, `~/dp2` and `~/dp3`.
