# The profit audit — seven questions, seven answers

Status: audit, decision-grade, written 2026-09-30 for the owner, who asked seven questions in
plain words and deserves seven plain answers. Nothing here switches anything on. No production
code was written, no commit was made, no order was placed, no bot, cron job, unit or console was
touched, nothing was written into `~/earn-run`, no trading endpoint was called, and `.env` was
never read. The only network calls were Binance's public catalogue and public candle data.

This replaces the earlier draft of the same name written at 10:46 today. Four independent
measurement teams ran during the day; the second round re-measured questions 2, 4, 5, 6 and 7
with different measurers, and several of the morning's numbers did not survive. Where a number
changed, only the corrected value appears, and §0 lists every change on one line each.

## How to read the numbers

Every figure carries a label:

- **[verified here]** — I re-derived it myself today on this machine, read-only.
- **[corrected]** — a measurer reported it and a reviewer changed it. Only the corrected value is
  used.
- **[ledger]** — measured in an earlier study and cited, not re-derived. Those studies are listed
  at the end.
- **[one measurement]** — measured once, not independently reproduced. Never used as the number
  that settles a question.

Three rules held throughout. **Costs are always on:** 0.30% for a round trip (10 basis points of
fee plus 5 of slippage per side), charged on every number. **A risk-adjusted score never appears
without its baseline and its window** — the score is the repo's arithmetic Sharpe, and "hold
Bitcoin and do nothing" is what everything is measured against. **A smaller fall is never
presented as a bigger return;** where a change buys one by giving up the other, both appear in
the same sentence.

Nothing in this document is a claim to have found an edge. After roughly 8,158 accumulated
attempts in this repository, the bar a new edge claim must clear is a score of about **2.32**
against buy-and-hold Bitcoin's **0.83**. The highest score measured anywhere in this round is
**1.121**. This round added **24** attempts; this document adds none.

---

## 0. Where a measurer was corrected

| reported this morning | corrected to | does it change an answer? |
|---|---|---|
| The pot is **19,964.54** of 20,000 | **19,957.45** at today's 14:00Z prices [verified here]; realised across all four databases is **−44.81** and that part never moves | No — the sign was always down. But this is the number billed as the answer to "am I making money", so it has to be the code's own output. Three reviewers refused the original independently |
| The two ledger resets hide **73.37 USDT** | **69.77 USDT** [verified here] — the two day-one losses exactly | No. The original mixed a reset gap with a price-marking gap |
| Awake-only benchmark: BTC **−2.36%**, the 31-pair basket **−0.77%** | **Not reproducible and not used.** Three rebuilds got BTC anywhere from −0.46% to −4.38% and the basket from −2.94% to **+4.27%** — the basket's *sign* flips on an arbitrary choice | Yes, by deletion. Nothing in this document rests on an awake-only benchmark |
| The coin list is produced by hand and **nothing is scheduled to produce it again**; `exit_only` is empty | **It is scheduled** — `ops/crontab` line 45, Sunday 18:00 Gulf, runs `ops/refresh_backtest_data.sh`, whose first command is `python -m ops.universe_refresh` [verified here]. `exit_only` holds **15 names**, not zero [verified here] | Yes. The real weakness is latency and uptime, not a missing job — so the fix is smaller than reported |
| The monthly −10% stop uses the worst-measured response: **−5.23% a year against +30.53%** | That pair belongs to a **−3%-a-day** trigger firing 66.7 times a year, not to a −10%-a-month trigger. **The monthly stop has no measured cost anywhere in this repository**, and the asymmetry with the daily stop *is* documented | Yes. Nothing condemns the one automatic circuit breaker, so nothing here argues for softening it |
| The entry gate is what holds exposure down in a rally | **Refuted.** The **30% volatility target** binds nearly twice as hard (+0.088 of average exposure when removed, against +0.049 for the gate) | Yes — it moves the fix from the gate to the volatility target |
| The risk gate has **17** checks (`CLAUDE.md`) / **26** (`paper-trading-review`) | **27** [verified here — I counted `CHECK_ORDER` in `strategies/riskgate.py:62`] | No conclusion changes. Two of our own documents are wrong |
| Buy-and-hold Bitcoin scores **0.83** | **0.827** on the full panel from 2017-08 and **0.964** on 2019–2026. Both are right for their span | Yes, for fairness: any book measured from 2019 must be compared against 0.964, not 0.83 |

---

## The seven answers, in seven sentences

1. **Are we making profit?** **No** — the pot is **19,957.45 of 20,000** after a week, no real
   money has ever been at risk, and the strategy we intend to ship scores **0.83 over nine years
   against buy-and-hold Bitcoin's 0.83**.
2. **What scenarios will lose us money?** The three biggest are ours, not the market's: our own
   stop sells the low and the price closes back above it on **57.4%** of breaches, the laptop is
   asleep **70%** of the time, and **nothing at all** checks that the dollar we price everything
   in is still worth a dollar.
3. **Are we analysing all coins on Binance — and is anything missed?** **No** — Binance trades
   **503** dollar pairs, we authorise **31**, and the strategy can actually buy **10**; the width
   is a defensible decision, but **six** of our own pairs are unbuyable because of one line of
   code and that is a defect.
4. **How are we different from traders?** We are slower, cheaper and obedient — **1.29 turnovers
   a year against the 250%+ that cost ordinary traders 7.1 percentage points a year** — and we do
   not make more money than holding Bitcoin.
5. **Will we make maximum profit from a rally?** **No, and we cannot** — we capture a median
   **22.8%** of a big rally against an arithmetic ceiling of **40%**, because a book that can only
   buy captures exactly the weight it holds.
6. **Are we selling at the right times?** **Mostly no** — and the surprise is that the dumb 10%
   stop is the best-timed sell in the system (the coin falls a further **6.5%** after it) while
   the clever 200-day trend flip sells into a market that is **3.3% higher** a month later.
7. **Are we buying right?** **No, and it barely matters** — the entry rule's day-picking beats
   entering one day later by **+1.18%** with a range of **−0.41% to +3.01%** that includes zero,
   and the value in the rule is being invested while the trend is up, not picking the day.

---

## 1. Are we making profit?

**No. The pot is 19,957.45 of the 20,000 it started with — down 42.55, or 0.21%, after a week.
None of it is real money, and nothing has ever been at risk.**

### 1.1 The number that settles it

I recomputed this myself rather than trust it, because the morning's figure was refused by three
separate reviewers. Reading all four bot databases read-only (and including their write-ahead
logs — a plain file copy shows an empty book and is how the morning figure went stale):
[verified here]

| | trades | realised |
|---|---|---|
| sleeve A, the first evening's database | 2 closed | **−42.21** |
| sleeve B, the first evening's database | 2 closed | **−27.56** |
| sleeve A, the current run | 9 closed, 1 open | **+12.96** |
| sleeve B, the current run | 9 closed, 1 open | **+12.00** |
| **all four** | **22 closed, 2 open** | **−44.81** |

Realised is **−44.81** and that part never moves. About 1,000 USDT is still open (4.153 Solana in
A, 0.00593 Bitcoin in B), so the last few USDT of the pot re-price every hour. Priced at the last
closed hourly candle, 13:59Z today, those two positions are worth **+2.26**, which gives the pot
**19,957.45 (−42.55, −0.21%)**. Every pot figure in this document carries its timestamp for that
reason.

### 1.2 The console is showing you the opposite, and that is the most urgent thing in this section

At 14:15Z the fifteen-minute ledger reads **20,023.28** with "+24.96 realised" — the card says you
are **up 23**. You are **down 43**. [verified here]

The gap is **69.77 USDT** that two ledger resets hide: the ledger restarted from the seed when the
bots were restarted, and the first evening's two losses were left behind in the old databases.
69.77 is exactly those two losses (−42.21 and −27.56). A measurer put the hidden amount at 73.37;
the extra 3.6 was a price-marking difference, not money. [corrected]

**A ledger that shows a loss as a gain is worse than no ledger.** This is not a strategy problem
and it is on the build list at item 7.

### 1.3 The honest split of that −44.81

| | amount |
|---|---|
| Two accidents on the first evening — neither a decision the strategy made | **−69.77** |
| Everything the strategy itself has chosen since the restart | **+24.96** |
| The two open positions, at 13:59Z prices | **+2.26** |
| | **−42.55** |

The two accidents, for the record. **−42.21** was a "sell everything" typed into the console at
21:58 on 23 September (there is exactly one matching audit row, `human:console autonomy.flatten`,
preceded by a denied attempt two seconds earlier). **−27.56** was sleeve B buying roughly double
what it was told — two order legs of about 1,980 each on Bitcoin within 3.7 seconds, and the same
on Ethereum — and then dumping the lot fifteen minutes later when it noticed it had no valid
instruction. **19.92 of the 69.77 was fees.** Both are visible in the order legs. [ledger,
reproduced]

Attributing those to the operator and to a plumbing fault rather than to the strategy is the right
call for judging the strategy. It is not a comfort: the money is gone either way, and a system
that lets a hand-typed flatten and a double-buy happen is a system whose losses are partly its
own.

### 1.4 The +24.96 is one trade

A single Avalanche trade, taken by both bots on 29 September, made **+48.04**. The strategy's
whole run is **+24.96**. So the other sixteen trades it chose sum to **−23.08**. [verified here]

One lucky trade is not a strategy. (The morning measure reported this same dependence when the
run stood at +41.75; a Solana exit at 07:00Z cost 8.39 in each sleeve and took it to +24.96. Both
figures were right for their moment; this is the current one.)

### 1.5 Was it us, or was it the market?

Over the same calendar week, costed the same way: Bitcoin fell **3.30%** (−660 on this pot) and an
equal slice of all 31 coins we watch rose **2.58%** (+517). Dollars did nothing. We beat Bitcoin
and we lost to the basket. [ledger, confirmed]

Read that beside the number that matters more: **on average about 2% of the pot was ever in the
market at all**, and only 1.5% in the current profile. [one measurement] We were barely playing.
Beating Bitcoin by 55 USDT while holding 2% of the pot is not evidence of anything; it is a rounding
error with a favourable sign.

There was also an attempt to compare only the hours the machine was awake. **It does not survive.**
Three independent rebuilds put Bitcoin anywhere from −0.46% to −4.38% and the basket from −2.94%
to **+4.27%** — the basket's sign flips on the arbitrary choice of how long a gap counts as
"asleep". [corrected] Nothing here rests on it, and nobody should quote one.

### 1.6 Nine bets cannot tell us anything

Both bots run identical rules under this profile, so they are not two opinions. Counting the same
pair in the same hour as one decision, the record is **nine independent bets**. The average bet
made **+2.76**, and the honest range around that average runs from **losing 5.28 to making 10.80**.
Zero sits comfortably inside it; the test statistic is **0.813** where about 2.0 is needed.
[ledger, confirmed]

For even a first weak read we would need about **107 bets** — roughly 25 days of actually being
switched on, or **79 calendar days** at the 30% uptime we have been managing. And that is the
friendliest possible reading: it assumes today's average is the truth. If the true average sits
anywhere in the lower half of that range, no sample size ever shows a profit.

### 1.7 What outweighs the whole week

Two numbers, and they both say no.

- **This exact configuration has its own costed backtest: 2,199 trades, −26.45%, with a 26.8%
  worst fall.** That is 244 times our live sample. Until the live record is big enough to argue
  with it, it is the better estimate of what this setting earns — and it says it loses. [ledger]
- **The strategy we intend to ship scores 0.83 over nine years against buy-and-hold Bitcoin's
  0.83** — a difference of +0.007, which is statistically nothing (p = 0.48). All 78 fixed-holding
  books tested across the three timeframes are at or below buy-and-hold. The one-hour rule earns
  **+0.022% per trade against the 0.300% it costs**, a 13.6-times shortfall. [ledger]

### 1.8 The two things that do make money, and neither is a strategy

Paying fees in BNB is worth **+0.215% of the pot a year**. Earning interest on idle dollars is
worth about **+0.5 percentage points of annual return per 1% of interest rate**. Both survived a
round of auditing that refuted twelve other hypotheses. They are arithmetic, not skill, and they
are the only two things in the whole rejection ledger that survived. [ledger]

### 1.9 Nothing real has ever traded

69 fills, every one stamped as a test fill; zero reconciliations against a real exchange; zero
mode transitions; `dry_run` true in both runtime overlays. [verified here] The machinery works,
the books reconcile to the cent, and the money is intact.

---

## 2. What scenarios will lose us money?

**The biggest losses are not market events. They are our own stop and our own laptop.** Twenty
scenarios were measured and priced. Three matter, in this order.

### 2.1 Our own stop sells the low, and the price comes back

Across the 31 pairs we trade there were **12,910 days on which the price dug 6% or more below the
previous day's close** — the depth of our own stop, and 20.3% of all days. **57.4% of them closed
back above that line the same day.** On Bitcoin alone that is **175 of 300** such days. [one
measurement]

Each one is a sale at the worst price of the day followed by a recovery: 0.30% of cost paid plus a
median **2.39%** of recovery handed away — **2.69% of the position**, about **19 times a year** on
Bitcoin. Our stop has no confirmation rule and no memory, so a one-second dip fires it, and
nothing in the code prevents a single breach from re-firing.

This is the largest expected cost in the whole register, and it is the one whose fix is entirely
inside our own code: require a *closing* price below the line rather than a flicker, latch the
breach so it cannot re-fire, and make the way back in something better than a two-hour cooldown.

### 2.2 The laptop is asleep when the stop breaks

**50 of the last 165 hours were awake — 30%.** [verified here, from the fifteen-minute ledger's own
ticks] When a 6% stop is breached and nobody is there, a fourteen-hour absence ends with Bitcoin a
median **1.46%** and a mean **2.00%** below the stop price; in the worst one case in a hundred,
**21.21%** below; at the very worst, **43.82%**. [one measurement]

**Nothing on this machine can notice, because the scheduler is asleep too.** The health check runs
every five minutes from cron, and cron hibernates with everything else. This does not need to be
clever; it needs to be somewhere else.

### 2.3 Nobody is watching the dollar, the venue, or a delisting

There is a complete, working checker for all three. `runs/features/venue.py` reads Coinbase,
Bitstamp and Kraken for the USDT/USD price, the exchange catalogue for a halted symbol, and the
delisting feed. **I checked every importer of it myself: a skill script, the demo-mode console
service, two modules borrowing its HTTP transport, and its own tests. No scheduled job, nothing in
the order path, nothing in ingest, nothing in the health check.** [verified here]

A 4% break in the dollar — the size seen in USDC on 11 March 2023, which closed at 0.9592 — costs
about **2% of the pot** on a book that is half cash, and at the same moment makes every price,
every limit and every stop distance the system reads wrong by the same amount. [one measurement]
This is the cheapest hole in the register to close, and closing it also covers a delisting while
we hold the coin and a venue halt.

**One correction here matters.** The register said nothing schedules the coin-list resolver. **It
does:** `ops/crontab` line 45 runs weekly at Sunday 18:00 Gulf and its script's first command is
the resolver; the script's own header explains that the refresh "has no cron line of its own: it
rides this job." [verified here] So the weakness is *latency* — up to seven days for a delisting
to reach us, and the one scheduled fire on 27 September happened to land inside the 63-hour sleep
— plus the fact that the sell-only list **blocks buying and sells nothing**. That is a narrower
and cheaper fix than "build the missing job".

### 2.4 The good news, and it is large

**Being spot-only, with no borrowing, avoids 11.56% a year** that an always-long Bitcoin
perpetual position paid in funding — **81.4% of the position over seven years**, paid on 85.8% of
all intervals, worst rolling year 33.56% — plus the risk of being wiped out entirely. Ethereum
13.79% a year, XRP 14.60%. [one measurement] **That is the best structural decision in the
system**, it is arithmetic rather than skill, and it should be said out loud more often than it is.

### 2.5 Worries you can retire

- **There is no weekend in crypto**, so there is no weekend gap. The only gap is our own downtime.
- **The data-freshness clock cannot be frozen fresh**, because it records the age of the *data*,
  not the time of the write, and returns "infinitely old" when the file is missing or unreadable.
- **The 30-minute staleness rule works.** It absorbed roughly 3,500 exchange transport errors in
  one week without refusing a single legitimate order. Of the 4,341 refusals in the live journal,
  the reasons are stale data 1,927, a correlation-to-Bitcoin cap 1,821, and staleness 590.
  [verified here]
- **A single coin blowing up inside the satellite sleeve is bounded at −5% of the pot** by the
  two-seat, 5% cap. Long-only spot cannot lose more than the position.
- **The 422 days the book spent above its own Bitcoin limit were, in the median, days it was
  winning** — worth +1.16% of the pot on average, with a worst single day of −21.72%. The
  order-path reviewer's refusal of the automatic trim was right on the merits. [one measurement,
  against the ledger's authoritative 422 of 3,326 days]

### 2.6 Two things you should simply know

**The red button stops buying. It does not sell.** I verified this: the function that decides
whether to flatten never reads the kill file at all. Kill blocks new entries and cancels resting
buy orders; every exit stays allowed. That is deliberate, and on the measurements better than
selling — which is exactly why §2.1 is the thing everything else rests on. Under kill, the stops
are all that protect the book. [verified here]

**And one defect nobody has filed.** The live journal holds **two refusals whose reason is
`nav_valid:AttributeError`** — an unhandled programming error reached the risk gate and was
recorded as a refusal. [verified here] It is not in the 39-defect ledger. It is small, and it is in
the safety layer.

---

## 3. Are we analysing all coins on Binance — and is anything missed?

**No. Binance is trading 503 dollar pairs today; we authorise 31; and the strategy can actually
buy 10 of them — 2% of the venue. The width is a defensible decision. Six of our own pairs being
unbuyable is not.**

### 3.1 The funnel, measured from the venue itself

I fetched Binance's public catalogue today: **756 dollar pairs exist, 503 are trading**, the other
253 suspended. [verified here] Here is what happens to them:

| stage | pairs | share of the venue |
|---|---|---|
| Dollar pairs Binance is trading today | **503** | 100% |
| Pairs we keep price history for | 108 | 21% |
| The watchlist the scanners look at | 107 | 21% |
| Pairs the safety check will authorise at all | **31** | 6.2% |
| Of those 31, marked sell-only | 15 | — |
| Pairs the safety check would let money into | 16 | 3.2% |
| **Pairs the strategy can actually offer a seat to** | **10** | **2.0%** |
| Pairs it actually holds | 2 (Bitcoin, Ethereum) plus at most 2 small satellites | 0.4% |

Read from the live configuration: 2 core, 6 "major", 23 satellite, 76 watchlist; 15 of the 23
satellites are sell-only, leaving 8 that can take a seat. [verified here]

**The narrowing is a liquidity decision, not an oversight.** One filter does almost all of the
work: a minimum of $1M a day of turnover removes 277 pairs in one step. We are not failing to
notice the other 470 coins; we are declining to trade coins too thin to buy and sell without
moving their own price. [one measurement]

### 3.2 The one thing that is actually broken

**Six of the 31 pairs — DOGE, NEAR, SOL, TRX, XRP and ZEC — carry a 0.15 cap in the live
configuration, so the safety check would allow them, and the strategy can never buy them.**

I verified this in the code. `strategies/SleeveA.py:172` builds its list of candidates as
`[a for a in states if a not in core and cfg.universe.is_satellite(a)]`, and `is_satellite` is
exactly `tiers[a] == "satellite"`. A coin in the "major" tier is neither core nor satellite, so it
is **never offered a seat**. Six pairs are whitelisted, capped, priced and monitored — and
structurally unbuyable. [verified here]

**106 of the 422 big rallies in our own whitelist happened in those six coins.** [one measurement]
Chasing rallies has been measured and refused (see §3.4), so this is not a claim about lost money.
It is a defect: six pairs are unbuyable for a reason nobody chose and nobody has written down.

### 3.3 Is anything genuine missed?

Almost nothing. Of the pairs we ignore, only a handful clear our own quality bar — volatility
under 100% a year, listed at least three years, at least $10M a day — and four of them are USDC, a
euro pair, another dollar token and tokenised gold. **One real coin is missed: BNB**, $62M a day,
the third-largest non-stablecoin dollar market on the venue, comfortably past the age and
liquidity bars. It is untradeable because one line of `config/earn.yaml` marks it "candles for fee
conversion only; NEVER tradeable". [one measurement]

That is a policy, not evidence. It may well be the right policy — BNB is the token of the venue we
trade on, which is a real concentration argument — but it should be a decision somebody makes on
purpose rather than a line nobody re-reads. Everything else missed is coins younger than our
three-year age floor, and that floor is well supported: across 633 listings the typical new coin
is down about 48% after its first 180 days.

**And a number worth pausing on, pointing the other way: 10 of our own 31 pairs fall below the
$10M/day bar our own research specified.** DOT is the thinnest at $5.26M, then FIL, INJ, HBAR,
PENGU, BCH, FET, TRUMP, ONDO and XPL. Bitcoin is $1,069M. [one measurement] The whitelist is not a
list of names that clear our quality bar; it is a list that clears a much lower liquidity bar and
then ranks well. The filter lives in a document, not in the configuration that produced the
whitelist.

### 3.4 Would a wider list have earned more?

**We do not know, and two independent measurements disagreed about the sign.** One reported that
the coins we refuse would have scored 1.118 against our own list's 0.688 — a headline saying our
coin selection has been actively wrong. An independent rebuild got **the opposite direction in
every cut it ran**. No number from that study is quotable and none appears here. [corrected]

What both rebuilds agree on: **neither book beat holding Bitcoin**, and nothing came near the 2.32
bar. And the standing evidence against widening has not moved:

| what was tried | the measured result |
|---|---|
| Ranking coins by recent strength — the standard wide-book method | Rank correlation with future returns **−0.016 to −0.069** on three separate panels: a reliably *wrong* signal, not a weak one |
| A costed top-8 rotation on that ranking | **−13.5% a year with a −98.9% fall** — and it loses *before* costs |
| Buying dips across a screened basket, every way it can be built | **0 of 6,720** costed configurations beat holding Bitcoin |
| Spreading a signal that works on Bitcoin across a basket instead | Turns **+42.9% a year into −8.1%** |
| The base rate underneath all of it | In 2023-24 only **3.4%** of coins beat Bitcoin; the median coin passing a serious quality filter lost **7.65% over 30 days** and **18.81% over 90** |

All [ledger]. So: widening the list is not the answer, and this morning's attempt to prove our list
is the wrong one did not reproduce. Both are true at once, and neither is a reason to change the
list.

---

## 4. How are we different from traders?

**We are slower, cheaper and obedient — and we do not make more money.** On return, nine years
say the shipped book scores **0.83 against buy-and-hold Bitcoin's 0.83**, a difference that is
statistically nothing (p = 0.48). So the honest difference is not profit. It is cost and
behaviour, and there the gap is real and measurable.

### 4.1 The number that settles it

The best study of ordinary traders — Barber and Odean (2000), 66,465 households at a discount
broker — found something exactly on point: **the busiest traders and the calmest ones made almost
the same money before costs, and 7.1 percentage points a year apart after them** (11.4% against
18.5%). In their words, "there is very little difference in the gross performance". Frequency was
the whole difference. [external source, read in the original]

Our shipped book turns over **1.29 times a year** and pays **0.19% of the pot** in fees. It makes
about **four sell decisions a year**. It cannot revenge-trade, cannot panic-sell into a bad day —
that instinct measures **−5.23% a year against +30.53% for sitting still** at the same trigger —
and cannot be talked into the coin our own data says loses 7.65% a month. [ledger]

**That is what this system is for: it harvests the 7.1 points a human loses to themselves.** It is
a discipline product, not a return product, and it should never be sold as the second one.

### 4.2 Three honest problems

**First, the profile running on your laptop right now is not the system I just described.** It
trades every hour across 31 coins, and at its measured pace it would turn over **633% of the pot a
year — 2.5 times the worst-performing group in that study**. Its own backtest is −1.29% per 30
days with fees larger than the loss. Same codebase, one line of configuration, opposite ends of
the most reliable finding in this literature. [ledger] Switch it off.

**Second, we are not as disciplined as the claim.** The book drifts **above its own 40% Bitcoin
limit on 422 of 3,326 days — 12.7% of all days** — and reached 92% of the pot invested on 12 May
2021, the day before the crash. The mechanism is not a bug in the safety check: **the check
validates orders, and a position that grows past its cap places no order**, so there is nothing to
refuse. A person who wrote themselves a 40% limit and then held 50% for 422 days would be called
undisciplined. [ledger]

**Third, we are worse than an attentive person at noticing that the world changed.** Nothing checks
the dollar (§2.3, verified). A delisting takes up to seven days. And in April 2021 the book
carried **89% of the pot into a −53% fall and took 74 days to halve that**, losing 25.1% where the
same plan re-sized daily lost 1.4%. [ledger]

And a fourth, measured this week: **a discipline machine that is switched off 70% of the time is
not keeping any discipline at all.** 50 of 165 hours. [verified here]

### 4.3 Where we are simply the same as a human

We do not beat the market. We cannot pick winners. Our good weeks are noise — statistical
separation from holding Bitcoin on this evidence takes decades, not weeks. And we are on the wrong
side of a bar we set ourselves: after ~8,158 attempts the honest threshold is **2.32** against
Bitcoin's 0.83, and the best figure measured anywhere in this repository is **1.32**. [ledger] A
human trader does not know that bar exists, which is the one respect in which our honesty is a
real advantage — we know we have not won.

### 4.4 Three of our own documents are out of date

The safety check has **27** checks; `CLAUDE.md` says 17 and `paper-trading-review` says 26. The
coin-list refresh is **Sunday 18:00 Gulf**, not the Saturday 04:00 one measurer read (that line is
a different job entirely). And the loss register says nothing schedules the refresh, when
`ops/crontab` line 45 does. [verified here] None changes a conclusion. All three mean a reader who
trusts a document instead of the code will be slightly wrong — and a document that is wrong stops
being evidence.

---

## 5. Will we make maximum profit from a rally?

**No, and we cannot. We capture a median 22.8% of each big rally, and the ceiling is arithmetic,
not skill.**

### 5.1 The structural fact first, because it removes most of the argument

A book that can only buy, holding weight *w* of the rallying coin with the rest in dollars earning
nothing, ends a rally of size *R* at **1 + w × R**. **Its capture ratio is exactly *w*.** No
signal, no model and no amount of cleverness can raise it, because the book cannot borrow, cannot
short and cannot hold more than its cap. [ledger, and simple arithmetic]

So the ceilings are fixed: **40% for a Bitcoin rally, 30% for Ethereum, 70% if both run together,
2.5% for a single satellite.** The configured 80% gross limit is unreachable — the only caps above
5% are 40% and 30%, and they sum to 70% — which also means the 20% cash floor can never bind.

Against that ceiling, a median capture of 22.8% is **59% of the theoretical maximum** and **86% of
what a book that actually holds those weights and rebalances achieves** (rebalancing sells the
winner all the way up, so its own ceiling is only a median 25.1%). [one measurement, with an
independently rebuilt rally definition that found the same 21 rallies with identical dates]

### 5.2 Where the misses come from, in order of size

1. **The position is too small.** Median weight held during a rally is **12.3% against a 40% cap**
   — 38% of what is permitted. And the binding constraint is **the 30% annual volatility target,
   not the trend entry gate**: removing the volatility target raises average exposure by 0.088 and
   median capture to **31.4%**, while removing the entry gate raises it by only 0.049 and capture
   to 23.6%. The pre-registered expectation that the gate binds harder was **refuted**. [corrected]
2. **We were completely out of the market on 24.3% of rally days** — in one Ethereum run of +99%,
   on 100% of them, because the 200-day average had not turned back up.
3. **Exiting early is not the problem.** Seven exits inside six of the 21 rallies; 15 of 21 had no
   exit at all.
4. **273 of 422 big rallies were in coins the strategy cannot buy**, 106 of them by the defect in
   §3.2.

### 5.3 The number that settles it

All on the same span (2019–2026), the same costs, a 20,000 pot:

| book | 20,000 becomes | score | worst fall | legal under our own rules? |
|---|---|---|---|---|
| **what we shipped** | **120,143** | **0.916** | −45.8% | yes |
| the most our own mandate legally allows — 40% Bitcoin, 30% Ethereum, rebalanced | **280,695** | **0.969** | −61.3% | yes |
| buy and hold Bitcoin | **453,580** | **0.964** | −76.6% | the benchmark |

[one measurement] **We captured 43% of what our own rules legally allowed, while taking 75% of its
worst fall.** And the 160,552 we gave up did **not** buy a better risk-adjusted result: 0.916
against the ceiling's 0.969 and Bitcoin's 0.964. The only thing it bought is a shallower worst fall
— 45.8% instead of 61.3%. That is the trade, stated in one sentence, and it must not be restated as
a return improvement.

Two guardrails on that table. The baseline on this span is **0.964, not 0.83** — 0.83 is Bitcoin's
score on the full panel from 2017-08, which includes the 2018 bear market. Both are right for their
window, and mixing them flatters whichever book starts later. [corrected] And the 280,695 ceiling
is a panel simulation that **has never been run through the real strategy mechanics or the safety
check**. It measures what the caps permit on one realised path with a −61.3% fall attached. If
anyone later quotes it as a discovery, they are misquoting it.

---

## 6. Are we selling at the right times?

**Mostly no — and the surprise is which exit works.** The dumb, mechanical exits are the good ones
and the clever one is the bad one, and two completely unrelated samples say so.

### 6.1 Nine years of simulation

After each kind of sell, what did the coin do next? [one measurement]

| what sold it | times | booked | the coin, 30 days later | did selling avoid a fall? |
|---|---|---|---|---|
| **the 10% stop** | 25 | −12.5% | **−6.51%** | **80% of the time, yes** |
| **the 200-day trend flip** | 20 | +13.6% | **+3.26%** | 65% |
| **the monthly −10% breaker** | 7 | +129.2% | **+2.84%** | 29% |

**The dumb 10% stop is the best-timed sell in the system**: after it fires, the coin falls another
6.5% over the next month, and four times out of five selling avoided a further fall. **The clever
200-day trend flip is the worst**: after it sells, the market is 3.3% *higher* a month later on
average — it catches the slow bear markets and gets whipsawed by the fast ones. And the monthly
breaker fires *after a good run* (it books +129% on average, so it is selling winners) and the
market is higher at one day, one week and one month, every time. It is a circuit breaker, not a
strategy; it should not be judged as one, and it should not be widened on the belief that it is
protecting anything either.

### 6.2 One week of live paper says exactly the same thing

I checked all 22 closed paper trades myself. [verified here]

| what sold it | trades | net | won |
|---|---|---|---|
| mechanical profit target | 3 | **+51.58** | **3 of 3** |
| trailing stop | 7 | **+33.68** | **7 of 7** |
| **the trend-loss sell (`exit_signal`)** | **8** | **−60.31** | **0 of 8** |
| the human's "sell everything" | 2 | −42.21 | 0 of 2 |
| the coin left the tradeable list | 2 | −27.56 | 0 of 2 |

**Mechanical profit-takes: ten trades, +85.26, ten of ten won. The judgement-based sell: eight
trades, −60.31, none of eight won.** Twenty-two trades over six days is noise on its own, and it is
quoted here only because it agrees with nine years of simulation from a completely different
direction. Two methods, one answer: the mechanical exits are the good ones.

### 6.3 And we barely sell at all

**5.7 sells a year** — 52 in 9.11 years. (An earlier study counted 40 on a different construction;
the two differ in the satellite sleeve and in bookkeeping, and the order of magnitude is the
finding. Both agree it is about four to six a year.) Almost everything that could sell is switched
off: the profit ladder is empty, the profit table is off, the trailing stop is off, the volatility
stop is off, and the daily loss response is "hold". [ledger]

**Selling late cost 1,169 USDT per 20,000 in the median bad fall and 2,571 in the worst.** April
2021 is the worst case: the book fell **34.0%** where a version that trimmed as the trend weakened
fell **8.4%**. [one measurement]

### 6.4 The fix, and exactly what it costs

The signal to fix this already exists and is already computed — a fifteen-member trend ensemble —
and **it is wired only to buying.** Wiring it to selling, measured two ways by two teams:

| | shipped | with the trend trim |
|---|---|---|
| annual return | **19.5%** | **14.7%** |
| risk-adjusted score (baseline: Bitcoin hold 0.964 on this span) | 0.866 | **1.121** |
| worst fall | −34.7% | **−20.6%** |
| days above its own Bitcoin cap | 422 | **0** |
| fees | 0.24%/yr | 0.29%/yr |

**Read that honestly: it makes less money, not more.** It gives up 4.8 percentage points of annual
return to halve the worst fall and to stop breaking its own limits. That is a drawdown improvement
bought with return, and anyone who describes it as a way to make more money is misreading the
table. It is also still far below the 2.32 bar, so it is not an edge claim.

**It is not deployed.** I verified it: the runtime strategy file has no trim at all (`grep -c
"_trim_plan"` returns 0) while the working copy has it. [verified here] The order-path reviewer
refused it for four named reasons — no latch, the largest de-risk can be silenced by the monthly
fee budget, a resting buy order can fake a breach, and it would let the rules sleeve sell while the
kill switch is engaged. **Those four objections are still true, and they are the work.** A de-risk
that the safety check can refuse on a bad day, or that sells under kill, is worse than none.

---

## 7. Are we buying right?

**No, and it barely matters.** The entry rule's day-picking is worth nothing you could measure.

### 7.1 The number that settles it

At 30 days, entering on the rule's day beats entering one day later by **+1.18%** — with an honest
range of **−0.41% to +3.01%**, which includes zero. And a coin picked at **random** from the same 31
pairs on the same day did **0.48% better** than the rule, with a range of −5.31% to +5.56%. Both
pre-registered tests were **refuted**. [one measurement, with pre-registered falsifiers]

The reason is sample size, and it is not fixable by waiting: **in 7.7 years the rule made about 33
genuine timing calls on Bitcoin and Ethereum.** That is the whole sample. No statistic computed on
33 events can settle whether entry timing works, and none here claims to.

What the rule is actually for is **being invested while the trend is up, not choosing the day.** The
one timing statement the data does support is that entering a *week* late is measurably worse — and
that is a statement about being invested, not about precision.

### 7.2 The one real benefit

**On Bitcoin and Ethereum only, the gate cuts the chance of a worse-than-−20% month from 17.9% to
14.1%**, and earns 1.75 points more over 30 days. On the whole reachable set it is *backwards* — the
days it sits out have higher forward returns. So the gate is a **risk filter on the two core coins**,
not a return filter, and it does not travel to the other coins. [one measurement]

### 7.3 Where the buying is genuinely worse

The seven-day calendar top-up buys, on average, **3.5 points less over 30 days and 19.7 points less
over 90 days** than a coin drawn at random from the same list on the same day, and unlike the
headline above, that gap is statistically solid. It is not a timing failure — it is the *selection*:
the rule only ever tops up Bitcoin and Ethereum, and a random draw is free to pick a high-beta
altcoin on a day the whole market is rising. [one measurement]

**That is the honest answer to "are we missing the upside": in an up-market, yes, by design.** It is
the same fact from the other direction as "only 3.4% of coins beat Bitcoin", and the measured
alternative — actually chasing those coins — loses money (§3.4).

### 7.4 And in the live week

The fast profile's entries were under water at four hours in **20 of 22 trades** (mean −0.82%). Its
own measured edge is **+0.022% per trade against the 0.300% it costs** — a 13.6-times shortfall.
**It is not losing because it picks badly. It is losing because it pays.** [ledger, reproduced live]

One more thing about every live figure in this document: the paper run charges **0.200% a round trip
with no slippage** where the real floor is **0.300%**. On the volume actually traded that is
**18.49 USDT** never charged — 44% of the morning's headline profit. Every live number here is an
upper bound. [ledger]

---

## 8. The build list, in priority order

Each item is tied to a number above. The marking matters: by this repository's own tier rules,
**nine of these ten items are human-only.** Claude cannot fix any of them, because every one
touches the order path, the schedule, or the configuration. That is by design, and it means this
list is your work, not the machine's.

| # | do this | why — the number | who |
|---|---|---|---|
| 1 | **Stop the one-hour `fast-test` profile.** | §4.2: 633% turnover a year, 2.5× the worst quintile in the canonical study; its own backtest −1.29%/30d with fees larger than the loss; §7.4: entries under water in 20 of 22 trades at four hours. One line of config. The plumbing has been proven; nothing else is being learned. | human (`config/**`) |
| 2 | **One scheduled job that checks the dollar peg, the venue and delistings.** | §2.3: the checker is written and **no scheduled job imports it** (verified); a 4% dollar break is −2% of the pot *and* makes every price wrong at once. Closes three register rows for one job. | human (`ops/**`) |
| 3 | **Decide the six unbuyable pairs, then write the decision down.** | §3.2: DOGE, NEAR, SOL, TRX, XRP, ZEC are capped 0.15 and can never take a seat; 106 of 422 whitelist rallies are in them. Either make the "major" tier reachable or drop them from the whitelist. Today it is neither, and nobody chose that. | human (`strategies/**`) |
| 4 | **A stop that needs a closing price below the line, plus a latch.** | §2.1: 57.4% of 6% breaches close back above the line the same day (175 of 300 on Bitcoin), 2.69% of the position each, ~19 times a year. Largest expected cost in the register, entirely inside our own code — and the precondition for item 6, because under kill the stops are all there is. | human (`strategies/**`) |
| 5 | **A watchdog that lives somewhere other than this laptop.** | §2.2: 50 of 165 hours awake; a 14-hour absence after a breach ends Bitcoin a mean 2.00% below the stop, 1-in-100 at −21.21%. Nothing inside a sleeping machine can raise an alarm. | human (`ops/**`) |
| 6 | **Resolve the four objections to the trend trim, then ship it — as a drawdown change.** | §6.4: worst fall −34.7% → −20.6%, cap breaches 422 days → 0, score 0.866 → 1.121 against Bitcoin's 0.964 — **and return 19.5% → 14.7%**. Blocked on: no latch, fee-budget-silenceable, a resting buy can fake a breach, sells under kill. Item 4 does half the work. | human (`strategies/**`) |
| 7 | **Make the console pot tell the truth.** | §1.2: the card reads 20,023.28 while the true cumulative pot is 19,957.45; two resets hide 69.77. A ledger that shows a loss as a gain is worse than no ledger. | human (`console/**`) |
| 8 | **Generate the whitelist from the filter the research actually specified.** | §3.3: 10 of our 31 pairs are below the $10M/day bar our own research requires; DOT is $5.26M against Bitcoin's $1,069M. The filter lives in a document, not in the config that produced the list. | human (`config/**`) |
| 9 | **Fix `nav_valid:AttributeError`.** | §2.6: two refusals in the live journal caused by an unhandled programming error reaching the safety check. Small, unfiled, and it is the safety layer. | human (`strategies/**`) |
| 10 | **Correct the three stale documents.** | §4.4: the gate has 27 checks (`CLAUDE.md` says 17, the paper review says 26); the refresh is Sunday 18:00 not Saturday 04:00; the loss register says nothing schedules the refresh and cron line 45 does. | human (`CLAUDE.md`, `docs/**`) — the only item Claude can help with |

Items 1 and 2 are today's work. Items 4 and 6 are the ones that change what the system *is*.

---

## 9. What we would have to stop believing, if we wanted more return

These are not bugs. They are the four choices that define the mandate, and each one has a measured
price. If you want more return, you have to reopen one of them deliberately — not hope a better
signal appears.

**1. "No shorting."** Believing we can profit *from* a fall means shorting. Today the only thing
that happens in a fall is that we hold less because the asset fell, not because we sold. That is the
mechanism behind the flat result: **0.83 against Bitcoin's 0.83**. The century of evidence behind
trend-following, which is the intellectual case for our own signal, gets much of its crisis
performance from being able to be *short* — which our mandate forbids. Giving up shorting means
giving up the crisis half of the strategy we claim to be running.

**2. "No leverage."** Believing we can have Bitcoin's return at half Bitcoin's risk means
borrowing. **Giving that up is measured to be worth 11.56% a year** in funding we never pay — 81.4%
of the position over seven years — plus never being liquidated. This is the single best decision in
the system and the one I would least want changed.

**3. "Spot-only, long-only."** This is arithmetic, not preference: a book holding weight *w* of the
rallying coin ends a rally at **1 + w × R**, so **its capture is exactly *w***. 40% Bitcoin plus 30%
Ethereum is a **70% ceiling**, which is why the 80% gross limit and the 20% cash floor can never
bind. **If you want more than 40% of a Bitcoin rally, the only lever is the cap** — not a better
signal, not a better model, not more coins. And the cap has a price: the mandate-legal ceiling made
280,695 instead of 120,143, with a **−61.3% worst fall instead of −45.8%**.

**4. "Thirty-one pairs."** Believing the missing return is in the other 470 coins has been measured
nine separate ways and refused every time: the ranking signal has the **wrong sign** on three
panels, a costed rotation returns **−13.5% a year at −98.9%** and loses before costs, **0 of 6,720**
dip configurations beat holding Bitcoin, spreading a Bitcoin signal across a basket turns **+42.9%
into −8.1%**, and only **3.4%** of coins beat Bitcoin in 2023-24. Widening the list is not the
lever. The six coins we already have and cannot buy are a defect; the ones we never listed are a
decision the evidence supports.

**The choice in front of you, stated once.** The honest product here is **Bitcoin-like exposure at
roughly half the fall, with your own hands tied.** It is not more money than Bitcoin, and nine years
of our own measurement say it will not be. The only two levers that give more money are the caps —
which come with a −61.3% fall instead of −45.8% — and leverage, which the mandate forbids and which
is worth 11.56% a year before it liquidates you. **Everything in between has been measured and
refused, and that ledger is what this document is built on.**

---

## 10. Honest limits

1. **Nine independent bets is not a result.** The honest range on the average bet runs from losing
   5.28 to making 10.80; the test statistic is 0.813 where 2.0 is needed; and the sign rests on one
   Avalanche trade. No risk-adjusted score should be quoted from this sample and none is.
2. **The "107 bets" figure is the friendliest possible reading and is not a plan.** It assumes
   today's average is the truth. If the true average sits anywhere in the lower half of today's
   range, **no sample size produces a profit.** The honest sentence is: this sample cannot
   distinguish the result from zero.
3. **The two sleeves are not two experiments.** Under this profile they run identical rules — same
   pair, same hour, within seconds, to the cent. Counting 22 rows as 22 trades overstates the
   evidence by about 1.8 times. I collapsed them; anyone quoting "22 trades" has not.
4. **The pot moves.** Realised −44.81 is fixed; about 1,000 USDT is still open, so the last few
   USDT re-price hourly. The 19,957.45 headline is the 13:59Z mark. Every pot figure here carries
   its timestamp, and this is exactly how the morning's figure went stale.
5. **The paper run is cheaper than reality by 0.10% a round trip** — 18.49 USDT never charged, 44%
   of the morning's headline profit. It also assumes the quoted price is available in full. Every
   live figure is an upper bound.
6. **Fifty awake hours out of 165 is not a week of trading**, and the awake-only benchmark that was
   supposed to fix that does not survive: three rebuilds got Bitcoin from −0.46% to −4.38% and the
   basket from −2.94% to +4.27%, with the basket's sign flipping on an arbitrary threshold. Nothing
   here rests on it.
7. **Everything about the deployed book's behaviour is inferred from nine years of panel history
   and from reading the code, not from live evidence.** The live record is 22 closed paper trades,
   four of which are two accidents.
8. **The whipsaw cost is an upper bound.** The per-event figure (2.69% of the position) is solid;
   the events-per-year figure counts every day the price dug 6% below the prior close, not every day
   we actually held a position with a stop 6% under its entry. Four real trades cannot tell us how
   often that is true.
9. **The 22.8% rally capture is not a decimal to quote.** It moves to 18.7% at a 20% rally
   threshold and 13.2% at 30%. The *shape* — a capture in the twenties against a ceiling in the
   thirties and forties — is robust.
10. **The 280,695 mandate-legal ceiling is not a recommendation.** One realised path, a −61.3% fall
    attached, never run through the real strategy mechanics or the safety check.
11. **Per-coin entry-timing cells are 14 to 18 observations.** Forward windows overlap heavily, so
    even the 9,069-observation pool holds far fewer independent observations than its size suggests.
    Where the honest answer is "not measurable", this document says that rather than "zero".
12. **Whether a wider coin list would have earned more: we do not know.** Two rebuilds disagreed on
    the sign, so no number from that study appears here.
13. **Four figures here come from a single measurement and were not independently reproduced**: the
    2.69% whipsaw cost, the fourteen-hour-absence distribution, the liquidity and quality funnel in
    §3, and the funding figures in §2.4. All are labelled [one measurement], and none is the number
    that settles a question on its own.
14. **The comparison with human traders is a comparative, not an experiment.** There is no control
    group of people trading this book. The cited studies describe other people in other markets in
    other decades; only the transferable axes — turnover, frequency, cost — are compared, never
    outcomes. One intended source could not be read and is cited for no number.
15. **Trials.** This round added **24** selection trials, taking the cumulative count from ~8,134 to
    **~8,158**. The deflated hurdle stays at about **2.32** against buy-and-hold Bitcoin's **0.83**.
    The highest score anywhere in this round is **1.121**. **This document adds none and claims no
    edge.** Every finding above is either a diagnostic of the deployed book or a code fact, and
    neither needs a hurdle because neither is a claim about beating the market.

---

## Sources

**Verified by me on this host today, read-only:** the four bot databases (`~/earn-run/ft_userdata/
{a,b}/tradesv3.sqlite` and `runs/test-{a,b}-000.sqlite`, copied through sqlite's backup API so the
write-ahead logs are included) · `~/earn-run/journal/journal.db` (`nav_points`, `fills`,
`gate_decisions`, `reconciliations`, `mode_transitions`) · `~/earn-run/config/riskgate.json` and
`freqtrade-a.json` · `~/earn-run/config/earn.yaml` · `~/earn-run/strategies/{SleeveA,riskgate}.py`
· `strategies/riskgate.py` (`CHECK_ORDER`, `is_satellite`) · `strategies/SleeveA.py:172` ·
`ops/crontab` · `ops/refresh_backtest_data.sh` · every importer of `runs/features/venue.py` ·
Binance's public `api/v3/exchangeInfo` and `api/v3/klines`.

**Today's measurement reports:** `evals/research/profit-audit/loss-scenarios.md` ·
`timing-and-rally.md` · `vs-traders.md` · `pa1-ledger-2026-09-30T054945Z.md` and the `pa1-out-*`
outputs.

**The rejection ledger — cited, not re-derived:** `docs/design/exit-and-horizon-2026-09-29.md` ·
`audit-and-research-2026-09-29.md` · `growth-audit.md` · `dip-strategy.md` · `crisis-policy.md` ·
`trend-ensemble.md` · `trend-trim-2026-09-30.md` · `wide-universe.md` · `analogue-timing.md` ·
`ml-forecast.md` · `paper-trading-review-2026-09-29.md` · `profit-gaps.md` ·
`outage-2026-09-25.md` · `local-model-choice.md`.

**External, read in the original:** Barber & Odean (2000), *The Journal of Finance* 55(2):773–806 ·
Barber, Lee, Liu & Odean (2014), *Journal of Financial Markets* 18:1–24 · ESMA product-intervention
notice, 27 March 2018 · S&P DJI U.S. Persistence Scorecard, year-end 2024 · Hurst, Ooi & Pedersen
(2017), *JPM* 44(1):15–29 (abstract only; the full text was unobtainable, so no return or
risk-adjusted figure is taken from it).
