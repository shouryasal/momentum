# How Earn differs from a human trader — an honest comparative

Status: measured and read 2026-09-30, workspace `pa5`. This is MEASURE 5 of the profit audit.
Nothing here was switched on. No production code was written, no commit was made, no bot,
cron job, unit or console was touched, nothing was written into `~/earn-run`, no order was
placed, and the only network calls were the public `api/v3/exchangeInfo` endpoint.
Arithmetic script: `evals/research/profit-audit/vs_traders_arith.py` (lint-clean, imported by
nothing). **Selection trials added by this measure: 0.** No backtest was run; every Earn
figure is either cited from a design document on the rejection ledger or read read-only out
of the live databases.

Every published figure about human traders below was **read in the source document**, not
taken from a search summary. Where a source could not be read, it is named as unread and not
cited (§8, limit 4).

---

## 0. The answer, in one paragraph the owner can act on

**Earn is not a better trader than you. It is a different thing entirely: a small, slow,
two-coin exposure dial with a 27-check clerk in front of it.** On return, nine years of
measurement say it does not beat holding Bitcoin — the shipped book scores 0.83 risk-adjusted
against buy-and-hold BTC's 0.83, statistically indistinguishable (p = 0.48). What it does
better than a person is arithmetic and obedience: it turns the book over **1.29× a year**
against the **75%** of an average retail household and the **over 250%** of the busiest
quintile that Barber and Odean measured losing 7.1 percentage points a year to turnover alone
(2000), and it pays **0.19% of NAV a year** in fees where the busiest of those households paid
enough to end at **11.4% net against the calmest quintile's 18.5%** on gross returns the paper
calls barely different. It never revenge-trades, never averages down off a
feeling, and never sells because it cannot sleep — the three behaviours that the published
record says cost individual investors most. Against that: it is **strictly worse** than a
competent human at noticing that the world changed. Nothing in any of its 15 scheduled jobs
ever checks whether USDT is still worth a dollar, though the code to do it exists and is
wired to nothing; a delisting takes up to **7 days** to reach it; and in April 2021 it carried
**0.89 of NAV** into a −53% fall and took **74 days** to halve that, where the signal it
already computes would have taken 37. It also drifts **above its own position caps on 12.7%
of all days**, so the discipline claim is real but not total. **So: do not keep this system to
make more money than Bitcoin, because nine years say it will not. Keep it — if you keep it —
as the thing that stops *you* trading, and only after the one change all the measurement
points at (let the trend signal shrink a position, not just grow one: 0.83 → 1.12
risk-adjusted, −45.8% → −20.6% drawdown, and the cap breaches go to zero) and after somebody
wires up the peg check. Until both are done you are holding a slower version of Bitcoin with
an unmonitored stablecoin under it, and that is a worse trade than simply owning Bitcoin.**

---

## 1. What this system actually is, read from the code on 2026-09-30

Before comparing it to anything, here is what it is, verified by reading rather than by
trusting the documents. Two things surprised me and both are recorded here.

| what | verified value | where I read it |
|---|---|---|
| Assets it can hold as core | **2** — BTC, ETH | `config/earn.yaml:298` `base_weights: {BTC: 0.40, ETH: 0.30}` |
| What decides the size of a core position | a **15-member** trend ensemble: 9 own-price SMAs (50…250, step 25) and 6 Donchian channels, equal-weighted, one-bar lagged, output 0…1 | `runs/features/trend.py:116-123`. Counted, not quoted: `MA_LOOKBACKS` has 9 entries, `DONCHIAN_CHANNELS` has 6 |
| Where that signal is allowed to act | **buy side only** in the running code | `strategies/earn_base.py`; and see the next row |
| Can the running SleeveA make a position smaller? | **No.** Its entire sell signal is one line — a 200-day MA flip with 2% hysteresis | `~/earn-run/strategies/SleeveA.py:95`, `dataframe.loc[dataframe["regime_1d"]...== 0, "exit_long"] = 1` |
| Is the ensemble trim deployed? | **No.** `grep -c "_trim_plan" ~/earn-run/strategies/SleeveA.py` returns **0** | the runtime tree. The Windows working copy *does* have `_trim_plan` and `_sleeve_adjust` asks for it first — so it is built and not shipped, exactly as the brief said |
| What the bots are actually running right now | **`SleeveFast`, timeframe `1h`, 31 pairs, `dry_run: true`** | `~/earn-run/config/freqtrade-a.json` |
| Orders validated before sending | **27** deterministic checks, in a fixed order | `strategies/riskgate.py:62` `CHECK_ORDER` — I counted the tuple |
| Discretionary exits are gated by | 3 of those checks (`orders_per_day`, `turnover_day`, `fee_budget`) | `riskgate.py:90` `EXIT_CHECK_ORDER` |
| Risk exits (stop, flatten, KILL) | **never** blocked, including under KILL | `riskgate.py:1062` `check_exit` returns `GateDecision(True, ...)` unconditionally |
| Does any scheduled job check the USDT peg? | **No.** 15 cron jobs, none of them a venue job | `ops/crontab` job names; and see the next row |
| Does the peg-checking code exist? | **Yes, and it is orphaned.** `runs/features/venue.py` has a full multi-source peg checker (Coinbase / Bitstamp / Kraken USDT-USD, `exchangeInfo`, `system_status`, the delisting CMS feed). Its only importers are a skill script, a demo-mode console service, and two modules that borrow its HTTP transport | `grep -rn "features import venue"`. **Nothing in `runs/ingest.py`, `ops/healthcheck.py`, `strategies/` or any cron job imports it** |
| How fast can a delisting reach the bot? | **up to 7 days.** The deep universe refresh is `cron: "0 4 * * 6"` — once a week, Saturday 04:00 Gulf | `config/earn.yaml:540`. (The exit-and-horizon document says "Sunday 18:00"; the shipped cron says Saturday 04:00. Weekly either way.) |

**Two corrections to our own documents, found by counting.** `CLAUDE.md` says the risk gate is
"17 checks"; `paper-trading-review-2026-09-29.md` says 26. The shipped tuple has **27**. And
`SleeveA`'s exit line is at `:95`, not the `:92` the exit-and-horizon document quotes — the
file has moved. Neither changes a conclusion; both are the kind of drift that makes a document
stop being evidence.

**The sentence that matters most in this section:** the book that has been measured for nine
years (4h SleeveA, 40 sells, BTC+ETH) **is not the book that is running** (1h `SleeveFast`, 31
pairs, whose own profile header calls it a plumbing test and whose own costed backtest is
−1.29% per 30 days). Every favourable number in this report describes the configured system.
Every number from the live databases describes the other one.

---

## 2. The published evidence about human traders, read not summarised

| # | source, read | the finding I use |
|---|---|---|
| E1 | **Barber & Odean (2000)**, "Trading Is Hazardous to Your Wealth", *The Journal of Finance* 55(2):773–806, April 2000. Read: the PDF's abstract and §I from `faculty.haas.berkeley.edu`. | 66,465 households at a large discount broker, 1991–1996. Verbatim: *"those that trade most earn an annual return of 11.4 percent, while the market returns 17.9 percent. The average household earns an annual return of 16.4 percent … and turns over 75 percent of its portfolio annually."* And from §I: gross return, average household **18.7%**; net **16.4%**; highest-turnover quintile net **11.4%** against the lowest quintile's **18.5%** — with *"very little difference in the gross performance"* between them. Households that trade most average *"more than 250 percent"* turnover a year. |
| E2 | **Barber, Lee, Liu & Odean (2014)**, "The cross-section of speculator skill: Evidence from day trading", *Journal of Financial Markets* 18:1–24. Read: the PDF abstract from `faculty.haas.berkeley.edu`. | Taiwan, 1992–2006. Top-500-ranked day traders go on to earn **61.3 bps/day before fees, 37.9 after**; bottom-ranked **−11.5 before, −28.9 after**. Verbatim: *"Less than 1% of the day trader population is able to predictably and reliably earn positive abnormal returns net of fees."* |
| E3 | **ESMA product-intervention notice, 27 March 2018**. Read: the ESMA press release. | Across EU national-authority studies, verbatim: *"74-89% of retail accounts typically lose money on their investments, with average losses per client ranging from €1,600 to €29,000."* |
| E4 | **S&P Dow Jones Indices, U.S. Persistence Scorecard, data as of 31 Dec 2024**. Read: the PDF's summary page. | Verbatim: *"Among top-quartile funds within all reported active domestic equity categories as of December 2020, not a single fund remained in the top quartile over the next four years."* And: *"Only 2% of All Large-Cap Equity Funds Remained in the Top Half Over a Five-Year Period"* — against a 6.25% random baseline the report computes itself. |
| E5 | **Hurst, Ooi & Pedersen (2017)**, "A Century of Evidence on Trend-Following Investing", *The Journal of Portfolio Management* 44(1):15–29. Read: the abstract on the CBS Research Portal (see §8 limit 3 — I did not obtain the full text). | Trend-following studied across global markets since **1880**. Time-series momentum delivered positive average returns in **every decade since 1880**, and performed well in **8 of the 10 largest crisis periods** of the century, defined as the largest drawdowns of a 60/40 portfolio. |

What E1–E4 establish, taken together, is narrow and strong: **the published damage to retail
traders is overwhelmingly a cost-and-frequency story, not a stock-picking story.** E1 is the
cleanest version — same gross performance, 7.1 percentage points a year of difference in net,
produced by turnover alone. E2 says skill exists and is vanishingly rare. E3 says on leveraged
products the great majority simply lose. E4 says even the professionals who win do not repeat.

What E5 establishes is the category Earn belongs to: it is a long-only, spot-only,
single-asset-class **trend follower**, and trend following is a strategy with a century of
out-of-sample evidence behind it. That is the strongest thing that can honestly be said for
Earn's design — and §5 says why it does not rescue the return case.

---

## 3. Axis by axis

### 3.1 What decides a trade

| | Earn | a human trader |
|---|---|---|
| the decider | **15 rules voting**, equal-weighted, nothing fitted and nothing selected (`runs/features/trend.py`) | discretion |
| what it can see | price only, on 2 coins, on the last *closed* daily bar | price, news, narrative, the order book, a friend's opinion, a feeling |
| what it can express | one number, 0 to 1: how much of a 0.40/0.30 target to hold | anything, including leverage and shorts |
| parameter risk | removed by construction — the ensemble exists *because* one integer of moving-average lookback swings CAGR by 44 points (`trend-ensemble.md` §2) | unmanaged; a human picks 50 or 200 for reasons they cannot audit |
| its real weakness, stated | the 15 members are worth about **1.23 independent bets** (`trend-ensemble.md` §2, §10.2 item 7). If trend following stops working, all fifteen fail on the same day | a human can change their mind |

The honest reading: Earn's decision machinery is better than a human's *at the thing it does* —
it cannot talk itself into a different lookback, and it cannot be persuaded by a story. But
"15 members" sounds like 15 opinions and is measured to be worth 1.23. Do not buy the number
15.

### 3.2 How many decisions a year

| book | decisions / yr | source |
|---|---|---|
| **shipped 4h SleeveA — sells only** | **4.4** (40 sells over 3,326 days; the document rounds this to 4.1 — see §8 limit 6) | `exit-and-horizon-2026-09-29.md` §2 |
| shipped 4h SleeveA — every order | **14.4** (42 entries + 49 scheduled top-ups + 40 sells = 131 in 9.11y; it traded on 117 of 3,326 days) | same |
| the daily ensemble the docs justify | ~365 — it re-sizes every day | `trend-ensemble.md` §1 |
| **the 1h profile actually running** | **~2,190 round trips/yr** at the measured pace and 100% uptime (625/yr at its actual 31% uptime) | measured this run: 12 trades per sleeve over 48 awake hours |
| a day trader | by definition, many per day | E2 |

**Earn's shipped book makes about four sell decisions a year. The thing on the laptop right
now is making them 499× faster than that.** That single ratio is the most important number in
this report, because §3.3 shows that frequency is the whole mechanism by which retail traders
lose.

### 3.3 Turnover and fee drag — the axis where Earn genuinely wins

| | turnover | fee drag |
|---|---|---|
| **Earn, shipped 4h book** | **129% of NAV / yr** | **0.19% of NAV / yr** |
| Earn, the daily ensemble the docs justify | 862% / yr | 1.29% / yr |
| Earn, at a one-hour hold | **633% / yr** | (the reason the 1h profile loses) |
| Barber-Odean average household (E1) | **75% / yr** | enough to take 18.7% gross to 16.4% net |
| Barber-Odean busiest quintile (E1) | **> 250% / yr** | enough to take the same gross to **11.4% net**, against a 17.9% market |

Read the two Earn rows against the two human rows and the picture is uncomfortable rather than
flattering:

- The **shipped 4h book turns over 1.72× as much as the average retail household** and about
  **half** as much as the busiest quintile. It is not a low-turnover portfolio by household
  standards. It is cheap anyway, because it holds two large, liquid coins and pays 0.19%/yr —
  the households' drag came from spreads on small stocks, not only from frequency.
- The **1h profile running right now turns over 2.53× as much as the quintile Barber and Odean
  singled out as destroying 7.1 points a year**. It is, on this one axis, behaving like the
  worst-performing group in the canonical study of retail underperformance.
- And Earn's cost floor is harsher than theirs in one respect: **0.30% per round trip, always
  on.** 3.3 round trips is 1% of NAV gone. The 1h rule produces a measured **+0.022%** of gross
  edge per trade against that 0.300% — a **13.6× shortfall**. It is not losing because it picks
  badly. It is losing because it pays.

**This is where the system's whole case lives.** E1's finding is that gross performance barely
differs between the busiest and the calmest traders and net performance differs by 7.1 points a
year. A machine that cannot get bored, cannot be tempted by a rung, and turns over 1.29× a year
harvests exactly that 7.1 points — against *a human's* behaviour, not against the market. It is
a discipline product, not an alpha product.

### 3.4 The discipline a human cannot keep — and the 12.7% of days Earn does not keep it either

Things Earn cannot do, structurally, because no code path exists:

| discipline | why Earn keeps it | the verification |
|---|---|---|
| **no overtrading** | the gate counts trades, orders, turnover and fees per day and per month, and refuses the order that crosses a line | `riskgate.py` `trades_per_day`, `orders_per_day`, `turnover_day`, `fee_budget` |
| **no revenge trade** | the daily stop's shipped response is `hold` — lock entries, sell nothing. There is no "make it back" path | `earn.yaml:189`; `crisis-policy.md` §0 measured hold **+30.53%** CAGR against flatten **−5.23%** at the same trigger |
| **no position-size drift on entry** | every stake is clamped to the tightest of six headrooms before it is allowed to exist | `riskgate.cap_stake` |
| **no coin-picking on a hunch** | there is no coin-picking at all; the universe screen and the 2-coin core are config | `earn.yaml:298`; `growth-audit.md` |
| **no panic sell** | the one instinct a human has in a crash is the single worst policy measured in this system | `crisis-policy.md` §0 |
| **no discretionary override in the order path** | Claude never sits in it; 27 deterministic checks do | `CLAUDE.md` non-negotiables, verified in `riskgate.py` |

And now the part that must be said plainly, because it is the one place the discipline claim is
false:

| the limit it sets itself | what the never-trimming book actually did, over 3,326 days |
|---|---|
| BTC weight cap 0.40 | peaked **0.509**, over on **422 days = 12.7% of all days** |
| ETH weight cap 0.30 | peaked 0.607, over on 265 days = 8.0% |
| gross exposure ceiling 0.80 | reached **0.921**, over on 136 days = 4.1% |
| USDT floor 0.20 | pierced on 136 days |
| its own exposure-tracking check (±0.10 of the signal, every day) | **fails on 1,103 of 3,069 days = 35.9%**, worst deviation 0.803 of NAV |

The mechanism is not a bug in the gate. **The gate validates orders, and a position that grows
past its cap generates no order** — every cap is of the form `(position + stake) / nav ≤ cap`,
so there is nothing to refuse. A human who wrote themselves a 40% position limit and then held
50% for 422 days would be described as undisciplined. Earn is undisciplined in exactly that way,
for exactly that reason, and its own worst day in nine years is **2021-05-12 at 0.921 of NAV
invested — the day before the May-2021 crash.**

One more, measured this run and not flattering: **in the live test window the bots were awake
for 48 of 153 hours — 31%.** A discipline machine that is switched off two-thirds of the time is
not keeping any discipline at all. A human is at least awake when they are awake.

### 3.5 Universe breadth — narrower than a human's, and deliberately so

| | count | share of the venue |
|---|---|---|
| Binance USDT spot symbols **trading today** | **503** | 100% (read from `api/v3/exchangeInfo`, `permissions=SPOT`, this run; 716 USDT symbols exist, 503 in `TRADING`) |
| pairs the live bots may touch | **31** | **6.2%** |
| pairs with local candles, i.e. backtestable at all | 108 | 21.5% |
| tickers in the research panel (survivorship-free) | 747, of which **282 are dead** | — |
| pairs the book actually **holds** | **2** core, plus at most 2 satellites at 5% total | 0.4% |

A human scrolling a screener looks at more coins than this before breakfast. Earn looks at two.
That is not an oversight; it is the conclusion of the measurement: rank IC on trailing momentum
is **−0.016 to −0.069** (the wrong sign), a costed top-8 momentum rotation returns **−13.5%
CAGR at −98.9% drawdown** and loses *before* costs, only **3.4%** of coins beat BTC in 2023-24,
the median eligible altcoin does **−7.65% in 30 days** and **−18.81% in 90**, and **0 of 6,720**
costed dip/satellite configurations beat simply holding BTC (`growth-audit.md`,
`dip-strategy.md` §3.4). Where a human's breadth is an opportunity, nine years of this repo's
own measurement says it is a tax.

But state the cost of the choice honestly too: **a narrow universe cannot produce a rally
return.** `dip-strategy.md` §0.2's `s0.2` result is that a long-only spot book at full exposure
**cannot express upward conviction** — once it is fully invested it has nothing left to say. In
a coin rally, Earn's ceiling is "fully invested in BTC and ETH", and 2023-24 shows what that
costs: the ensemble made 53.7% while simply holding BTC made **137.6%** (`trend-ensemble.md`
§1). A human who bought one right coin beat it by an order of magnitude. Most humans did not
buy one right coin — but the ones who did, did.

### 3.6 What a human has that Earn does not

None of these is a defect to be fixed. They are the boundary of the mandate, and the owner
should know where it is.

| a human has | Earn does not, and here is the code reason |
|---|---|
| **context and news judgement** | Earn reads a whitelisted news archive with a two-source corroboration rule and turns it into blackout flags. It cannot weigh a story. Its own record: the `hack` keyword rule fired a full research run on the venture firm **"Hack VC"**, and the `lawsuit` substring rule labels the words "Security", "second" and "pursuit" — **31 rule labels at 51% precision** (`paper-trading-review-2026-09-29.md` §6 item 14) |
| **the ability to stop** | Earn has a kill switch, but **a human has to write it** (`ops/killdir/KILL`), and **KILL does not flatten** (G10). It can stop buying; it cannot decide the game is not worth playing |
| **discretion about the mandate** | Earn cannot short, cannot use leverage, cannot hold anything but spot, and cannot go to cash for a reason it was not given. `dip-strategy.md` §0.2 measures this as a hard structural ceiling, not a preference |
| **knowing when the model is wrong** | Earn's honest answer is `abstain: true`, which is better than a wrong answer and is not the same as judgement. In the live week **7 of 7 proposals abstained** on empty inputs |
| **noticing that nothing has happened for six days** | the holdings watcher read the wrong database for six days and reported `holdings: 0` in 100 of 103 cycles with positions open (`SL-05`). A human glancing at a screen would have seen it in one second |
| **caring** | Earn has no stake. That is the whole point, and it is also why it will ride a −53% fall without flinching (§3.7) |

### 3.7 Where Earn is strictly worse than a competent human

This is the section to act on. Each of these is something an attentive person with a phone
would do better, and each is cheap to fix relative to what it can cost.

| # | Earn is worse at | the measured size | why |
|---|---|---|---|
| 1 | **Noticing a delisting** | up to **7 days** | the deep universe refresh runs weekly (`cron: "0 4 * * 6"`), `universe.exit_only` is baked in at config-generation time, and it needs a container restart. G11: the flag schema is hard-wired to BTC/ETH, so a notice about any of the other 29 pairs can only block *everything* or nothing |
| 2 | **Noticing that USDT is not worth a dollar** | **never** | 15 cron jobs, none a venue job; `runs/features/venue.py` has a working multi-source peg checker and **nothing in the order path, ingest, the healthcheck or any cron job imports it**. Every price, limit and stop distance is quoted in USDT, so a 2% discount silently rescales all of them (G4) |
| 3 | **Getting out of a fast fall** | median **24 extra days** to halve exposure, up to **65** | it has no trim. In **April 2021 it carried 0.89 of NAV into a −53% fall, took 74 days to halve it, and lost 25.1% where the same targets re-sized daily lost 1.4%.** In March 2024 it took 114 days against the signal's 49 and absorbed 97% of the fall |
| 4 | **Selling at all** | **4.4 sells a year**, and the one discretionary sell it has **can be refused by a fee budget** | `exit_signal` is not in `RISK_EXIT_REASONS`, so SleeveA's only judgement-based sell passes through `check_discretionary_exit`'s three churn checks (G6). A de-risk that the gate can refuse on a bad day is not a de-risk |
| 5 | **Surviving a gap or a 30% candle** | unprotected | F4: the market-only-stop invariant does not hold on Binance spot — it becomes a `STOP_LOSS_LIMIT` 1% below the stop. In TEST the stop is a candle-close check, so one candle past it is unprotected. `near_stop`, the only fast path a no-news crash has, **can never be true** because it reads an empty table (G2) |
| 6 | **Telling anyone anything** | **775 alerts, 0 ever delivered** | AU3-02. While the owner is away the system cannot reach them. A human notices their own problems |
| 7 | **Being switched on** | **31% uptime** measured this window; 48 of 153 hours | the host is a laptop that hibernates on a closed lid |

**Items 2 and 3 are the ones that can cost the book rather than a few points of it**, and item 3
is the one with a measured fix already sitting in the working copy.

### 3.8 Where Earn is the *same* as a human, which is nobody's favourite finding

- **It does not beat the market.** Shipped book **0.83** risk-adjusted against buy-and-hold
  BTC's **0.83**; ΔSharpe +0.007, **p = 0.48**. E1's households did not beat their index either.
  Earn has built a much better-behaved way of not beating the benchmark.
- **It cannot pick winners.** rank IC the wrong sign; ML on direction not forecastable at a size
  that survives 15 bps; a single feature (`−vol_60`) beat the best ML model at 4 of 6 targets.
  E2 says less than 1% of day traders can predictably do it either. Earn is honestly in the 99%.
- **Its good weeks are noise.** The live fast profile is up on 20 trades; its own 30-day costed
  backtest is **−1.29%** with fees at 1.35% of balance. `dip-strategy.md` §10: statistical
  separation from holding BTC takes **86 years**, and distinguishing 1.14 from 0.83 at 80% power
  takes about **245 years**. E4's point about professionals applies here exactly: a good run is
  not evidence.
- **It is on the wrong side of a hurdle it set itself.** ~8,134 cumulative selection trials put
  the deflated bar at **~2.32**. The best honest figure measured anywhere in this repo is
  **1.32** (`exit-and-horizon-2026-09-29.md` §7 limit 6; a 1.44 also appears in
  `audit-and-research-2026-09-29.md` §2.3 but is explicitly a *hindsight* BNB result and is not
  a claim). Nothing has cleared it. A human trader does not even know this bar exists, which is
  the one respect in which Earn's honesty is a real advantage — it knows it has not won.

---

## 4. The one comparison that settles the positioning

Barber and Odean's 2000 table is the right frame, because it separates the two things people
confuse.

| | gross return | net return |
|---|---|---|
| E1, lowest-turnover quintile (75%/yr average overall) | ~same as everyone | **18.5%** |
| E1, highest-turnover quintile (>250%/yr) | ~same as everyone | **11.4%** |
| the market index, same period | — | 17.9% |

*"There is very little difference in the gross performance"* — their words. **7.1 percentage
points a year separated the two groups, and it was all cost and frequency.**

Now put Earn's two configurations on that axis:

| | turnover | what E1's evidence predicts |
|---|---|---|
| **Earn, shipped 4h book** | **129%/yr, 0.19% fees** | closer to the calm quintile than the busy one; and its measured result is indeed "matches the benchmark", which is what the calm quintile did |
| **Earn, the 1h profile running now** | **633%/yr** | 2.53× the busiest quintile. Its measured result is **−1.29% per 30 days, with fees exceeding the loss** |

**The same codebase sits on both ends of the most robust finding in retail-investor
research.** Which end it sits on is one line of configuration: `profiles.active`.

---

## 5. Does E5 rescue the return case? No.

It is fair to say Earn belongs to a category with a century of evidence: Hurst, Ooi & Pedersen
(2017) find time-series momentum positive in **every decade since 1880** and good in **8 of the
10 largest crisis drawdowns**. That is the intellectual warrant for the 15-member ensemble, and
it is a real one.

Three reasons it does not make Earn a profit case:

1. **The evidence is for a diversified, multi-asset-class, long/short futures programme.** Earn
   is **long-only, spot-only, two coins, one venue**, with 15 members worth **1.23 independent
   bets**. The crisis performance in E5 comes substantially from being able to be *short*, which
   Earn's mandate forbids.
2. **The deployed book does not implement trend following on the sell side at all.** The
   ensemble is read **only when buying** (verified in §1). A trend follower that cannot reduce is
   not the strategy E5 measured; it is buy-and-hold with a slow entry filter. The measured
   consequence is exactly that: 0.83 against BTC's 0.83.
3. **Even the correct implementation does not clear this repo's own bar.** The scale-out book
   scores 1.10–1.16 against a deflated hurdle of ~2.32, and the ensemble's headline edge over
   holding BTC is **1.33×, not the 1.86× this project quoted for months** — the old figure
   divided a geometric number by an arithmetic one (`audit-and-research-2026-09-29.md` §0).

So E5 justifies the *design*, and the repo's own measurement says the *implementation* throws
the design away. Those are compatible statements and both are true.

---

## 6. Honest positioning — what is this system FOR?

**It is a discipline device and a drawdown device, not a return device, and it is currently
neither because the trim is not shipped and the peg is not watched.** Nine years of measurement
say it will not beat holding Bitcoin on return: 0.83 against 0.83, p = 0.48, and the 78 fixed-hold
books tested across 1h/4h/1d are every one of them at or below buy-and-hold. What it can honestly
be for is the thing E1 priced at 7.1 percentage points a year — it removes the human from the
order path, turns over 1.29× instead of 250%+, pays 0.19% of NAV instead of enough to cost a third
of the return, and cannot revenge-trade, cannot panic-sell into a −3% day (the single worst policy
measured here, at −5.23% CAGR against +30.53% for doing nothing), and cannot be talked into the
coin its own data says loses 7.65% a month at the median. If the ensemble trim ships, it also
becomes a genuine drawdown device: **−20.6% instead of −45.8% worst drawdown, 0.83 → 1.12
risk-adjusted, and 0 days over its own caps instead of 422** — bought for +0.10 percentage points
of fees a year. That is a product worth having: *Bitcoin-like exposure at roughly half the fall,
with your own hands tied.* It is not a product that makes more money than Bitcoin, and it should
never be sold to you as one. So the decision in front of you is not "is this profitable" — it is
**"is half the drawdown and none of my own mistakes worth giving up some of Bitcoin's return?"**
If yes, ship the trim and wire the peg check, and stop the 1h profile today, because that one is
on the wrong side of the only robust finding in this literature. If no, hold Bitcoin and turn
the laptop off; that is a legitimate answer and nine years of our own numbers support it.

---

## 7. Trials added

**0.** This measure ran no backtest, fitted nothing, and selected nothing. The cumulative count
stays at ~8,134 and the deflated hurdle at ~2.32. Every Earn number here is cited from a design
document or read read-only out of the live databases and the shipped code; the only computation
was ratio arithmetic on those figures (`vs_traders_arith.py`).

---

## 8. Honest limits

1. **This is a comparative, not an experiment.** There is no control group of human traders
   trading this book. E1–E4 describe *other* people in *other* markets in *other* decades; the
   comparison is on axes (turnover, frequency, cost) that transfer, not on outcomes that do not.
   Crypto spot in 2017–2026 is not US equities in 1991–1996.
2. **I did not re-derive any Earn figure on the rejection ledger**, by instruction. The 0.83, the
   422 days, the 40 sells, the 1.29×, the 633%, the −53%/0.89-of-NAV April 2021 and the
   0-of-6,720 are all cited from `exit-and-horizon-2026-09-29.md`, `trend-ensemble.md`,
   `growth-audit.md`, `dip-strategy.md`, `crisis-policy.md` and
   `audit-and-research-2026-09-29.md`. If any of those is wrong, this report inherits it.
3. **E5 is cited from its abstract, not its full text.** I read the abstract and bibliographic
   record on the CBS Research Portal and the AQR landing page; the full article is paywalled and
   I could not obtain it from this host. I therefore take only the two claims the abstract
   states (positive in every decade since 1880; good in 8 of 10 crisis drawdowns) and **no Sharpe
   ratio or return figure** from it.
4. **One intended source went uncited because I could not read it.** Odean (1998), *"Are
   Investors Reluctant to Realize Their Losses?"*, would have supplied the magnitude of the
   disposition effect for §3.4's "no revenge trade" claim. My text extraction of the PDF returned
   nothing and I refuse to quote a figure I have not read, so the disposition effect is named as a
   behaviour and **no number is attached to it**.
5. **The live figures are a 7-day, 24-trade, 31%-uptime sample and prove nothing about return.**
   12 of those 24 rows are the same 12 decisions duplicated across two sleeves. The `edge-audit`
   skill would refuse a row count of 12, and so do I. They are used here only to establish the
   *pace* at which the running profile makes decisions, which a small sample can bound.
6. **My sells-per-year differs from the document's.** 40 sells over 3,326 days is **4.39/yr**;
   `exit-and-horizon-2026-09-29.md` says **4.1**. The difference is the year count used. I report
   mine and flag the discrepancy rather than silently adopting either.
7. **Three of our own documents are now out of date against the code**: the gate has 27 checks
   (`CLAUDE.md` says 17, the paper review says 26); `SleeveA`'s exit line is at `:95` not `:92`;
   and the universe refresh is Saturday 04:00 Gulf, not the Sunday 18:00 the exit document names.
   None changes a conclusion. All three mean a reader who trusts a document instead of the code
   will be slightly wrong.
8. **The 503 USDT-trading-symbol count is a single snapshot** taken this run from
   `api/v3/exchangeInfo`; it moves weekly with listings and delistings.
9. **I did not measure SleeveB or the trim.** SleeveB (Claude's proposals) already has a complete
   trim at `strategies/SleeveB.py:363-390`; the SleeveA trim is in the Windows working copy
   (`_trim_plan`) and **verified absent from the runtime**. Whether the order-path reviewer's four
   objections (no latch, the largest de-risk is fee-budget-silenceable, a resting buy can fake a
   breach, and it would let SleeveA sell under KILL) are resolvable is not a question this measure
   examined, and §0's recommendation to ship the trim is **conditional on them being resolved** —
   a de-risk that can be refused, or that sells under KILL, is worse than none.
10. **Nothing was written to the live runtime**, no bot, cron job, unit, console or order was
    touched, no commit was made, and `.env` was never read. The only network calls were
    `api.binance.com/api/v3/exchangeInfo` and the public paper/press URLs in §2. Every database
    was opened `mode=ro`.

---

## 9. Sources

**Read this run (external):**
[Barber & Odean 2000, *J. Finance* 55(2):773–806](https://faculty.haas.berkeley.edu/odean/papers%20current%20versions/individual_investor_performance_final.pdf) ·
[Barber, Lee, Liu & Odean 2014, *J. Financial Markets* 18:1–24](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/The%20Cross-Section%20of%20Speculator%20Skill.pdf) ·
[ESMA product intervention, 27 March 2018](https://www.esma.europa.eu/press-news/esma-news/esma-agrees-prohibit-binary-options-and-restrict-cfds-protect-retail-investors) ·
[S&P DJI U.S. Persistence Scorecard, YE 2024](https://requisitecm.com/pdf/SPIVA-US-Persistence-Scorecard-Year-End-2024.pdf) ·
[Hurst, Ooi & Pedersen 2017, *JPM* 44(1):15–29 — abstract only](https://research.cbs.dk/en/publications/a-century-of-evidence-on-trend-following-investing/)

**Cited, not re-derived (the rejection ledger):**
`docs/design/exit-and-horizon-2026-09-29.md` §1–§3, §5, §7 ·
`docs/design/audit-and-research-2026-09-29.md` §0–§2 ·
`docs/design/growth-audit.md` §0, §1.5 ·
`docs/design/dip-strategy.md` §0.2, §0.3, §3.4, §6.3, §10 ·
`docs/design/trend-ensemble.md` §1, §2, §3.2, §10.2 ·
`docs/design/crisis-policy.md` §0, §5.3 (G1–G17) ·
`docs/design/paper-trading-review-2026-09-29.md` §1–§6 ·
`docs/design/profit-gaps.md` §3 ·
`docs/design/analogue-timing.md` §4.4–4.5 · `docs/design/ml-forecast.md` · `docs/design/wide-universe.md`

**Code read and verified this run:**
`strategies/riskgate.py` (`CHECK_ORDER`, `EXIT_CHECK_ORDER`, `check_entry`, `check_exit`,
`cap_stake`) · `strategies/SleeveA.py` (`_sleeve_adjust`, `_trim_plan`, the `exit_long` line) ·
`runs/features/trend.py` (`MA_LOOKBACKS`, `DONCHIAN_CHANNELS`, `MEMBERS`) ·
`runs/features/venue.py` (and every importer of it) · `config/earn.yaml` (caps, schedules) ·
`ops/crontab` (job names) · `ops/lib/flags.py` · `~/earn-run/config/freqtrade-a.json` ·
`~/earn-run/strategies/SleeveA.py`

**Live data read `mode=ro`:** `~/earn-run/journal/journal.db` (`nav_points`, `fills`,
`gate_decisions`) · `~/earn-run/ft_userdata/{a,b}/tradesv3.sqlite` ·
`~/earn-run/ft_userdata/{a,b}/runs/test-{a,b}-000.sqlite`

**Public endpoint:** `api.binance.com/api/v3/exchangeInfo?permissions=SPOT`
