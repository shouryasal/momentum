# What we can add and improve — the whole check, 2026-09-30

You asked for a complete sweep: learn from every coin on Binance, improve buying and selling
and rallies and crashes, find out what happened to ML, ask whether the LLM could do more,
check how often we look, check whether we watch the right numbers, and find out why we miss
rallies. Six research tracks ran today, one per question, on static data only — no live
database was opened, because opening it this morning is the most likely cause of the 6h42m
window in which the bots could not enter.

**The short version.** Five of the six questions come back "no", and the reasons are measured
rather than argued. One comes back "yes, and here is a defect": the system computes a
satellite position it is then forbidden to take, two thirds of the time. And the largest known
hole in the whole system is one none of the six tracks touched — the selling side, where your
live record already has a clear sign: mechanical profit-taking is 10 trades for 10 wins and
+85.27; the trend-loss exit is 8 trades for 0 wins and −60.30.

Everything below is net of 0.30% a round trip, always. Every risk-adjusted number is the
repo's arithmetic Sharpe and is quoted against its baseline, because a Sharpe without a
baseline is a decoration.

---

## 1. Can we improve the rule by learning from every Binance coin?

**No.** Training the rule on every eligible coin instead of the 16 we can enter made it
**worse**, and the sign is the wrong way round from the hypothesis: wide-fit minus narrow-fit
out-of-sample Sharpe is **−0.126** (t −1.934, 9 folds), and the wide arm won only **3 of 9**
test windows. Both arms lose to holding BTC over the same window (wide 0.363, narrow 0.489,
BTC hold 0.518).

The mechanism is clean. By the last fold, the wide arm trained on **1,147** trades against the
narrow arm's **231** — five times the data — and still chose worse settings. The extra coins
are a different population: they are the ones our quality filter exists to exclude. Fitting on
them optimises for a market the book will never trade. We already knew trading them was a bad
idea (median −3.26% a year on the 383 pairs Earn ignores, against +18.40% on the 31 it may
authorise). Now we know learning from them is too.

One thing the wide universe did buy: **perfect stability.** It picked the identical settings in
all nine folds, while the narrow fit churned through five different sets, some chosen on barely
50 trades. Stability was the honest prize on offer — and the stable set is the **worse** set
(0.640 against 0.698 stitched out-of-sample Sharpe, and worse in every regime split). So we do
not get to keep it.

Two things you should know about how that answer was reached, because they change how much to
trust it. First, the first pass of this study produced Sharpe 1.6 to 2.3 and it was **wrong** —
the backtest was crediting each trade the overnight jump from one day's close to the next day's
open, which we never own because we buy at the open. Breakout entries gap up almost by
definition, so the bug flattered exactly the rules that looked best. Corrected, the best of 144
versions falls from 2.308 to **0.706**, and **0 of 144** beat holding BTC at 0.83. Second, the
test universe this was measured on is close in *count* to the real one (16.2 reconstructed
names against 16 live) but **wrong in composition**: two of the sixteen live names, ZEC and
NEAR, do not appear in the reconstruction, and ZEC is the single largest 2026 riser in the book.
So treat the H1 result as a firm direction with a soft magnitude.

---

## 2. Are we looking at all the right parameters — buy volume, volume, the rest?

**No, we are not — and after measuring the missing ones on 468 coins over nine years, we should
not start.** Three separate answers, all pointing the same way.

**What we look at today.** For about **87 of the 107** coins on the watchlist, Earn computes
exactly **six** numbers: price, yesterday's move, 20-day volatility, distance from the 200-day
average, drop from the 30-day high, and a news count. Only **20** coins get the full set of 25
(`DEFAULT_RICH_PAIRS = 20`). That is a token-budget decision, not a bug, but it is the honest
answer. Of those 25, **nine are read by no rule at all** — including order-book spread and
depth, which we record every 15 minutes and have never once looked at.

**Recent buy volume specifically.** Binance gives it away free in the same call we already
make, and we throw it out at ingest. So I fetched it. It correlates **0.991** with total
volume. It is total volume wearing a different label, and it adds nothing.

**The buy *share*** — what fraction of a bar was buying — is genuinely new (only 0.107
correlated with volume) and is the strongest thing found anywhere today. It is also useless to
us. In the ~380 coins we are refused from trading it is overwhelming (1h rank IC −0.0182 at
t −25.9). In the ~22 we may actually buy it fails every test written down in advance: the sign
flips between the two halves of the sample, it holds in only 2 of 5 time periods, and after
controlling for volume, volatility, age and reversal its strength is **1.08** against a
pre-registered bar of 2.0.

**And its direction is backwards from intuition.** Heavy buying predicts a **lower** next bar,
not a higher one. Anyone wiring "lots of buying means it is going up" would have wired it
upside down. The same is true of every attention-shaped number we have: volume spikes,
accelerating trade counts, rising volume trend — five features, all pointing at the **drop**,
nine of them surviving a strict multiple-testing correction, no sign flip between samples, no
regime flip. Being bought heavily is a warning light here, not a green light. The attention
arrives with the top.

**The peak-or-drop rule specifically does not work.** "Rising price on falling volume means the
rally is exhausted" predicts one thing; measured, it went the **other** way in the broad
universe (+0.454% over five days, t 3.11), and in the coins we can trade the effect is exactly
**−0.006% at t −0.02** — absent, on ~14,000 observations a side.

**The cost floor ends it.** A daily signal in our tradeable universe needs a predictive strength
of **0.0264** just to pay the 0.30% round trip. The best new feature manages **0.006** there,
and 0.019 anywhere. Every costed book is negative; the best is Sharpe **0.035** against BTC
hold **0.843**.

**The one thing worth doing needs no new data.** Plain 20-day volatility — already computed for
all 107 watchlist names, every cycle, and wired to nothing — ranks which coin is about to fall
20% in a week better than anything else tried, and better than the nine-model ML zoo. Blending
anything into it makes it worse. Use what is already there, alone. That is build item 5.

---

## 3. What happened to ML, and can it be improved?

**It was built, it was measured, and then nothing was ever connected.** `ml/` is 14 modules,
9,677 lines, 345 passing tests — and **nothing outside it imports it**, it is not in the
config, it is not on the schedule, and **0 of its own 11 build-list items shipped**. The same is
true of the project's *best* forecast: the volatility blend that scores out-of-sample R² 0.60
on BTC and 0.54 on ETH is computed only when a skill asks for it, and no part of the trading
code reads it. "We have a volatility forecast" and "our position sizes use a volatility
forecast" are two different sentences, and only the first is true today.

**Can it be improved? Not in the direction you are hoping.** The one honest thread left open was
a model that ranks which coins are about to fall hard. It closes as a negative, and the reason
is worth knowing.

The old headline — "AUC 0.617 against 0.543" — compared the model against the wrong yardstick.
Measured fairly, one number we already use (60-day volatility, as a plain ordering) scores
**0.6093**. The model's entire advantage is **+0.0072**, not +0.074. It is also **72% the same
thing** — the model's ranking and that one feature agree three-quarters of the time.

Then it was turned into a book, which is the only test that matters. Refusing the model's
riskiest tenth of coins made things **worse**: return fell 4.05 percentage points a year and
the drawdown improved by **0.00** percentage points (−85.47% both ways). Using the single
feature instead improved return by 8.8 points. Against its own honest control, the model
overlay is significantly worse at **t −3.1**.

The mechanism is the clearest finding in the study: the coins the model calls riskiest are also
**high-return** coins. Its worst decile lost 0.729% a forward week; the simple feature's worst
decile lost 0.930%. A −20% dip is one-sided in the label and two-sided in the money. The model
got better at spotting violence and worse at spotting bad holdings. **A higher score bought a
worse book.**

And on the two coins we actually trade, every version of this rule loses to simply holding, on
both return and Sharpe, on both assets. Three of eight make the **drawdown worse** — they
de-risk into the fall and miss the bounce. That is the trend-loss exit failure again, wearing a
new hat.

**On direction, here is the gap as a number rather than as discouragement.** BTC needs 53.57%
accuracy just to pay the fees, and 56.07% to match holding. The best directional signal ever
measured on BTC here is 0.123 with a t-statistic of 0.7 — not significant. A basket book would
need a cross-sectional strength of about **0.265**, which is **2.5×** the strongest such signal
in nine years of data. That gap is not closable by a better model. It is closable only by
cheaper trades, longer holds, or a different question.

**What to keep:** `ml/` as a harness, not as a model zoo. Its no-lookahead proof and its cached
nine-year panel are why today's study took an afternoon instead of a week. It has now paid for
itself twice, both times by producing a clean negative fast. Six of its eleven build-list items
should be struck (section 8).

---

## 4. How often do we look, and is that right?

**We are not too slow. It was measured, and "react faster" is dead.** But there is a real
cadence bug, in a place nobody was looking.

**Three clocks, and mixing them up is why the system feels sluggish.**

| clock | period | what moves on it |
|---|---|---|
| the bot loop | **~5 seconds** | a stop-loss, the kill switch, a risk flatten, a fresh proposal |
| the decision clock | **4 hours** (6×/day) | a buy, and the trend exit |
| the signal itself | **1 day** | the 200-day average SleeveA's entire entry and exit read |

Worst case from a price crossing the line to an order on the book is about **28 hours**;
average about **14**. The scanner looks **288 times a day**, but that funnels to at most **6**
validations, **5** model decisions and **4** trades. Claude analyses **twice a day** (08:30 and
16:00 Gulf), plus event-fired runs.

**Looking more often loses money.** Every hour instead of every 4 hours is **−0.75 percentage
points a year** and improves only **15 of 31** coins — a coin flip. Reacting to the live price
intrabar, the fastest thing physically possible, is **−1.7 to −2.4 points a year**, doubles the
round trips (17.5 → 33.5) and doubles the fee drag (5.25% → 10.05% of capital).

**Reaction lag is free.** Holding the signal and the turnover identical and moving only the
hour of execution: waiting 4 hours is very slightly *better* than acting instantly (+0.26
points, +0.007 Sharpe, 16 of 31), and waiting **48 hours** is better still (+3.95 points, and
the best portfolio Sharpe in the sweep at 0.816 against 0.736 at zero lag). The reason is
structural: **a 200-day average does not move much in two days.** You cannot be late to a
signal that slow.

Every cadence variant still loses to holding the same 31 coins (best variant 0.834 against
buy-and-hold 0.841). This is a cost observation, not an edge.

**Staleness is not a data problem.** Exchange outages cost about **one blocked decision in two
thousand** (0.049%). But both of this month's multi-hour entry blackouts were plumbing. Here is
the bug: the bot prices its own orders off live data every 5 seconds, yet its **permission** to
trade depends on a separate 15-minute cron job writing a small status file — and that file's
tightest feed, the order-book snapshot, has **no grace period at all** against a 30-minute
limit. Two missed cron slots stop every entry while the bot is staring at a perfectly fresh
order book. Every blocked minute is a minute in which the profit-taking that is 10-for-10
cannot run. That is build item 6.

**So: change nothing about the trading cadence. Fix the permission pipeline. And stop running
heavy analysis against the live databases while the bots trade.**

---

## 5. Can internet search, news and social sentiment help — and what else should the LLM do?

**No on sentiment, and no on internet search — the second one is not even a choice.** Claude
cannot search the web inside Earn, ever: `ALWAYS_DISALLOWED` in `runs/decision_core.py` is one
of the invariants no tier and no config can move. The only outside information is nine
whitelisted news feeds, ingested by code, under a two-source corroboration rule.

**Why every free sentiment source failed, specifically:**

- **Google Trends** returns a *different number every time you ask for the same past week*,
  because it samples searches and rescales each answer to its own window. A series whose past
  changes when you ask again cannot be backtested. It also refused the first request outright
  (HTTP 429).
- **Reddit's** public archive was shut on 2 May 2023.
- **GitHub** gives 52 weeks of history against a 730-day floor.
- **Fear and Greed** looks like sentiment, but **60% of its weight is arithmetic on price and
  volume we already compute**, it correlates **+0.428** with last week's return, and its survey
  component was quietly switched off mid-history. Its forward-return sign is also *backwards*
  from the folklore it is sold on (t +1.83, and not significant anyway).
- **Our own news archive cannot be rebuilt backwards.** Six of the nine feeds carry one to two
  days of items; only two are archived at the Internet Archive at all. It will be backtestable
  in a few years because it is accumulating now. It is not backtestable today.

**Wikipedia pageviews was the one clean source, and it was tested properly and failed.** On
Bitcoin and Ethereum over 3,289 days it does not predict the next week's return (t **+0.82** and
**+0.09**, against a bar of 2.0), and once ordinary trailing volatility is in the model it makes
the volatility forecast **worse** on BTC (−0.0047 incremental R² against a +0.020 bar). Across
16 coins it scores **0.502** on predicting a 20% weekly crash — a coin flip — while trailing
volatility alone scores **0.609**. Attention rises *after* price moves: its correlation with
the prior week's absolute return is +0.238. It is a chart of where the price has been.

**Now the part worth building, and it needs no sentiment data at all.** When Binance delists a
coin, the damage starts about a month early, not on the day. Across the **195** pairs that have
left the panel, the final **30 days** is a median of **−47.3%** and the final **7 days**
**−18.5%**, with 74% ending down. And this is not only dust — MATIC, FTM, MKR, EOS, XMR, TON,
AGIX, HNT and BTT all left. **Nothing in the risk gate's 27 checks looks at whether a symbol is
still trading, and nothing checks whether USDT is still worth a dollar.**

The shape that fits our rules exactly: the model reads the exchange notice and produces **four
fields** — symbol, event, effective time, source URL — and nothing else. No score, no
direction, no weight. Then **code calls Binance's own public API** and checks whether that
symbol really is still trading. If the API disagrees with the model, the claim is thrown away
and journaled as a miss. The model makes a guess that code can catch, which is the strongest
form of verification this system has. About **$2 a month**.

The USDT peg needs no model at all: we can read it off Binance spot from data we already
ingest. Since 2020 a 50-basis-point deviation has occurred **five times in seven years** —
exactly the profile a guard should have. Two design notes the data forces: use the close or the
median of three stablecoins, never the low (the −80% "low" in 2021 was a wick), and keep the
peg in the gate as a **refusal**, never in a prompt as a **view**.

**Three other LLM jobs pass the same test** (a model may only produce something code re-checks),
and all three are cheap because they ride jobs that already run: plain-words explanation of a
gate refusal on the console (it explains a record it cannot alter); post-mortem of a losing
trade against its frozen evidence pack (every claim must cite a feature key that exists);
drafting pre-registrations for the hypothesis lab (the one place the guard rail already exists
and is load-bearing). Two jobs are rejected outright: a sentiment score from social text
(nothing can check it for years) and a price or direction view from news text (direction is not
forecastable at a size that survives our costs — adding text does not change that).

---

## 6. Why do we miss rallies, and which misses are fixable?

**Because the mandate excludes the coins they happen in — not because we fail to see them.** A
forensics loop was built for exactly this question and it now attributes every rally to exactly
one named cause, with **zero** unattributable out of 2,605.

Over the last 24 months there were **2,605 rallies of +30% or more inside 20 days**, in **436 of
the 493** currently-listed USDT pairs. Rallies are normal, not rare.

**97.8% of them — 2,548 — happened in coins Earn is not allowed to buy**: too illiquid for a
tier (36.5%), outside the watchlist funnel (25.6%), listed under 180 days (16.9%), or a
satellite that fails the quality filter and is exit-only (15.1%). That share is 97.7% at a +20%
bar and 97.6% at a +50% bar, so it is a property of the system and not of where the line was
drawn.

Only **57 rallies — 2.19%** — reached a coin the sleeve could enter. Of those, **86%** were
refused by a trend gate doing precisely its job: the coin, or Bitcoin, was below its own
200-day line when the rally started. Removing that gate is not an improvement; an ungated
equal-weight book drew down **−96.6%** against the gated core's −50.9%.

**And the system sees about 90% of these rallies.** A detector would fire before 93.4% of them
in theory and 90.1% in practice. Detection is not the bottleneck. Relaxing the universe rules
one at a time — real strategy, no foresight, costs on — is worth **between −0.47 and 0.00
percentage points** over two years, because widening the funnel feeds a bottleneck rather than
the book.

**That bottleneck is the one thing that is genuinely broken.** The config asks for a satellite
position of **2.5%** of NAV. The volatility control shrinks it to a median **1.63%**. The risk
gate refuses to open anything under **2.00%**. So the system computes a position it is then
forbidden to take — **on 292 of the 430 days it wanted one, 67.9%**. The satellite half of your
book held something on only **9.5%** of its available seat-days, in 10 of 24 months, and
**nothing at all** from December 2025 through September 2026. On 2025-09-04, CAKE was the
top-ranked eligible candidate with a free seat and its own trend up; the position was deleted
by this rule; CAKE then rallied 30.9%.

Each of the three config numbers is defensible alone. The defect is in their product, which
nobody had computed. **Three unrelated ways of resolving it** — one seat instead of two, a
bigger satellite sleeve, or removing the floor — all land in the same place: **+16.2% to
+18.3%** over 24 months at Sharpe **0.618 to 0.682**, against the shipped **+12.47%** at
**0.515**, at the **same drawdown** (−11.6% to −12.3% against −11.9%). Three independent fixes
agreeing is why this reads as a mechanism and not as luck.

**It is still not an edge.** Every one of those books loses to simply holding Bitcoin over the
same window (**+32.98%**, Sharpe 0.542), and all of them sit far below the honesty hurdle. It
is the repair of a configuration that contradicts itself, and it should be argued on those
terms only.

**One more thing the loop found about itself, and it matters:** a position the strategy wanted
and the gate refused to open leaves a **log line and no database row**. So the single most
common fixable cause in the whole study is **invisible in the live system today**. That is
build item 3, and item 2 cannot be verified without it.

---

## 7. The build list

Ranked by measured value. Ten items. Every tier is per `CLAUDE.md`. "What would prove it in 30
days" is deliberately *not* a return number in any row, because 30 days cannot distinguish
Sharpe 0.68 from 0.51 — that needs decades, and saying otherwise would be the same mistake this
document keeps refusing.

### 1 — Finish the selling side: clear the three blockers on the trim that is already built

**Why first.** This is the strongest signal in the entire dataset and the only one with live
money behind it: mechanical profit-taking is **10 trades, +85.27, 10 of 10 won**; the
trend-loss exit is **8 trades, −60.30, 0 of 8 won**. The nine-year study said the same thing
from the other end — all 78 fixed-hold books sit at or below buy-and-hold, and the deployed
exit book is Sharpe 0.83 against BTC hold 0.83. The ensemble is wired to **buying only**, and
SleeveA's entire exit is one 200-day line. Three of today's six tracks independently concluded
that the exit is where the losses are. **None of the six measured it**, because all six held the
exit fixed — which is the largest single gap in today's work.

**The work is not research.** The trim is built, and `trend-trim-2026-09-30.md` pins 25 of its
properties with named tests. It is held on three named defects: no latch; the largest de-risk
goes down the discretionary branch the fee budget may refuse; and it would let SleeveA sell
under KILL. I confirmed two of the three textually — the fee-budget branch split is in the
trim's own test table, and the KILL behaviour is its limit 9, where the document argues it is
*deliberate* and matches the ladder and SleeveB. I could not locate the latch item in either
file. A human should read those two sections and decide.

**Tier 2** (`strategies/**`). **Who:** Shourya, in a normal session with tests.
**30-day proof:** every trend-driven reduction appears as a journal row naming which branch
fired; zero de-risks silenced by the fee budget; and the exit-reason table shows trend-driven
reductions no longer concentrated in the 0-for-8 bucket.

### 2 — Resolve the satellite sizing contradiction

**The only item today's work measured that recovers return the config is throwing away.**
2.5% requested → 1.63% after volatility scaling → refused below 2.00%, deleting 292 of 430
asset-days. Three fixes, one plateau: +16.2% to +18.3% at Sharpe 0.618–0.682 against the
shipped +12.47% at 0.515, same drawdown; BTC hold over the same window is +32.98% at 0.542, so
this closes a self-inflicted gap and does not beat holding Bitcoin. The arithmetic says the
cheapest consistent setting is **one seat at 5% gross**, not two.

**Tier 2** (`config/earn.yaml`: `risk.max_satellite_positions`, `risk.max_satellite_gross`,
`risk.min_position_pct_nav`). **Who:** Shourya, through the normal change gate, replayed by
`evals/verify_change.py`. There *is* a tier-1 route — `sleeve_a.vol.target_annual` is a
tier-1 knob bounded 0.10–0.50 with a 0.05 max step — but it is the wrong lever: on the measured
median scalar it takes **two** change cycles to reach 0.40 before the floor clears, and it
raises the volatility target of the whole book, core included, to fix a satellite bug.
**30-day proof:** satellite seat utilisation rises from 9.5% of available seat-days toward 50%+,
and zero positions are deleted by the `min_position` floor. Not a return number.

### 3 — Record the position the gate refused to open

`SleeveA`'s `instrument("want_none", …)` writes a log line and **no** `gate_decisions` row, so
item 2's biggest number cannot be seen in production. Four other columns are missing for the
same reason (what the scanner looked at and did not flag; candidates cut before the screener;
the universe snapshot in force at an instant; which satellites held the seats).

**Tier 2** (`ops/sql/migrations/` — which stops at 005 today — plus `strategies/SleeveA.py`).
**Who:** Shourya. **30-day proof:** the table exists and carries rows, and the journal's count
of deleted asset-days matches the replay's 67.9% rate within a stated tolerance.

### 4 — A venue-status and USDT-peg refusal in the gate

The gate has **27** checks today and not one of them asks whether a symbol is still trading or
whether USDT is still worth a dollar. Delisting costs a median **−47.3%** over the final 30 days
and **−18.5%** over the final 7, 74% of them negative, across 195 pairs. **This is a
tail-avoidance item, not a return item** — it will show no return benefit and should not be sold
as one. Notably, the trim document's own follow-up table ranks this gap **above the trim
itself**.

**Tier 2** for the check (`strategies/riskgate.py`, `ops/lib/flags.py`, and the `venue-guard`
skill's `scripts/**`); **tier 1** for the notice-reading prompt (`prompts/**`). **Who:** Shourya
for the gate; a Claude worktree session may propose the prompt through `changes/*.json`.
**30-day proof:** the check runs on every decision and logs a pass; the peg gauge reports a
daily deviation; and a replay against a known past delisting (MATIC or FTM) shows `exit_only`
would have been set **at least 7 days before** the last print. Do not expect a live fire — the
peg gauge has triggered five times in seven years.

### 5 — Wire the volatility feature we already compute as a risk ordering, and nothing else

Four independent samples now agree that plain trailing volatility, as a daily cross-sectional
percentile, orders "which coin is about to fall 20% in a week" better than the hand-set flag
(0.543) and better than the nine-model ML zoo (0.617): **0.654** on the tradeable universe with
5 of 5 purged folds and its best fold the most recent; **0.6093** as the honest control in the
ML re-run; **0.609** on an independent 16-name panel; and **0.634** from a small logistic on
ranked daily features. It costs nothing: `vol_ann_20d` is already computed for all 107 watchlist
names, every cycle, and read by no rule.

**Three hard constraints, or this becomes item 12's mistake.** (a) It **tightens exposure only**
— it must never authorise a buy. (b) Do **not** replace it with a model: the model form makes
the book significantly worse at t −3.1. (c) Do **not** apply it as a BTC/ETH timer: every
time-series version loses to holding on both assets, and three of eight make the drawdown
worse. Its home is the satellite cross-section — **which means it depends on item 2**, because
the satellite sleeve held a seat on 9.5% of its days and a ranker with nothing to rank is not a
feature. One measurement caveat: the three studies may not define the −20%/7-day target
identically, and that must be checked before the 0.654-versus-0.617 comparison is quoted to
anyone.

**Tier 2** (`runs/signals/detectors.py` for a detector, `strategies/` for a sizing input).
**Who:** Shourya. **30-day proof:** the ordering is computed and journaled daily for every
eligible name, and a grading table records each day's realised 7-day drawdown against that
day's decile. Not a return number.

### 6 — Give the order-book freshness stamp a grace period, or let the bot stamp its own

`book_snapshots` is a blocking freshness source with **no grace period**, written by a
15-minute cron, checked against a 30-minute limit — **two cron slots of headroom** — while the
candle feeds get a one-timeframe grace and are effectively never stale. Market data is stale on
0.049% of decision moments; both multi-hour blackouts this month were this pipeline. Three
options, cheapest last: let the thing that *has* the live order book stamp its own freshness;
or widen the book grace to one cadence period; or write book snapshots more often than ingest
runs.

**Tier 2** (`ops/lib/freshness.py`) — and it is a safety property, so explicitly not a tier-1
tweak. **Who:** Shourya. **30-day proof:** zero entry-blocked minutes attributable to a missed
ingest slot, against two multi-hour blackouts in September.

### 7 — Fix the trial counter's lost-update race

`ml/registry.py: Trials.add` still mutates an in-memory snapshot and saves with no re-read and
no lock — verified unfixed today. This is the one mechanism that stops the project fooling
itself about how many things it has tried, and every honesty hurdle in every document above
depends on that count.

**Tier:** `ml/**` is named in **neither** the tier-2 pattern list nor the tier-0 writable list,
so the hook does not block it and it goes through the normal change gate. A human should
confirm that classification is intended. **Who:** either, through `changes/*.json`.
**30-day proof:** a concurrency test fails before the fix and passes after; the cumulative count
is monotone and equals the sum of declared additions.

### 8 — Stop the system treating volume and buying as bullish

Five volume and buy-pressure features, one sign, nine surviving a strict correction, no regime
flip, no sample flip: they all predict the **drop**. If any prompt or scan stage reads rising
volume as confirmation, it is reading it backwards. And `volume_spike` currently labels a
direction of UP when the spiking bar's return is positive — **drop the label rather than invert
it**, because the cross-sectional effect behind the inversion does not survive in the coins we
can actually trade.

**Tier 1** for the prompts (`prompts/**`); **tier 2** for the detector label
(`runs/signals/detectors.py`). **Who:** a Claude worktree session may propose the prompt change
through `changes/*.json`; the detector is Shourya's. **30-day proof:** no proposal in
`daily_review` cites rising volume as bullish confirmation, and `volume_spike` emits no
direction.

### 9 — Make the missed-rally forensics loop a standing job

It works: 0 of 2,605 rallies unattributable, and the funnel replay lands within one name of the
live config. Run weekly, it turns every miss into evidence instead of a feeling — which is
exactly what you asked for. It needs item 3 to see its own biggest finding, and it must read a
snapshot handed over under the ops lock, **never** the live file while a bot is trading.

**Tier 2** (`evals/**` and a schedule entry in `config/earn.yaml`). **Who:** Shourya.
**30-day proof:** it runs weekly without touching a live database, every rally in the window is
attributed to exactly one cause, and the unattributable bucket stays at 0.

### 10 — Ingest the two free taker-buy columns. Collect only; wire nothing.

`takerBuyBaseVolume` and `takerBuyQuoteVolume` are fields 9 and 10 of the klines call we
already make — no key, same endpoint, same rate-limit weight — and they are stored **nowhere**:
not in the panel, not in the feather files, not in the feature builder. Today's buy-side study
had to use a **proxy** for one of its five volume findings. **I expect this to produce another
negative** (the real features were already measured at 1.4× to 13× below break-even), and it is
last for that reason. Its value is that it closes the question with the real column and stops
future runs re-litigating it with a proxy, and the archive becomes usable later.

**Tier 2** (`runs/ingest.py`, `ops/sql/knowledge.sql`). **Who:** Shourya. **30-day proof:** both
columns populated on every bar with no gaps, and the proxy finding re-tested against the real
column. **No detector, no prompt slot, and no hypothesis without a pre-registered falsifier.**

---

## 8. What not to build, and why — so none of this is proposed again

| do not build | the measured reason |
|---|---|
| Trade more coins / widen the universe | 383 ignored pairs carry a median **−3.26%** a year against +18.40% on the 31 authorised; and relaxing the universe rules one at a time measures **−0.47 to 0.00** points over two years, because widening feeds the sizing bottleneck (deleted asset-days rise 292 → 553 / 616 / 622 / 1,812) |
| Train the rule on all coins | Wide minus narrow out-of-sample Sharpe **−0.126**, t −1.934, wide wins **3 of 9** folds, on **5×** the training trades |
| A faster decision clock, or an intrabar trigger | 1h evaluation is **−0.75** points a year and improves 15 of 31 — a coin flip; intrabar is **−1.7 to −2.4** points, and doubles both round trips and fee drag |
| Chase reaction speed, or "fix" the 4h-versus-1d mismatch | Delay is free out to **48 hours**; the shipped 4-hour lag is **+0.26** points and +0.007 Sharpe *better* than acting instantly. Changing `trading.timeframe` to `1d` would be a cosmetic change sold as an improvement |
| Google Trends, Reddit, Fear & Greed, GitHub activity, Wikipedia pageviews | Unbacktestable by construction (Trends resamples and rescales; Pushshift revoked 2023-05-02; GitHub gives 52 weeks against a 730-day floor) or refuted (Wikipedia: forward-return t +0.82 / +0.09, cross-sectional IC −0.0005 at t −0.04, drawdown AUC 0.502 against volatility's 0.609). F&G is 60% price arithmetic, correlates +0.428 with last week's return, and its formula changed mid-history |
| Any new buy-side or microstructure feature as an **entry** signal | Break-even predictive strength is **0.0264**; best measured in the tradeable universe is **0.006**. Every costed book is negative; best Sharpe **0.035** against BTC hold **0.843** |
| Order-book spread and depth | Recorded every 15 minutes, read by nothing, and there is no history to test (748 rows). Keep accruing; revisit at two years. Meanwhile stop showing them as if they were signals |
| A direction forecast, from ML or from news text | BTC needs **53.57%** accuracy to pay the toll and **56.07%** to match holding; best ever measured is 0.123 at t 0.7. A basket book needs **2.5×** the strongest cross-sectional signal in nine years |
| Calibrating the drawdown model's probabilities | Isotonic on a purged inner fold made it worse on **every** axis: gap 0.0656 → 0.1037, AUC 0.6165 → 0.6006, Brier 0.1603 → 0.1702. The probabilities cannot be used as a size multiplier, raw or calibrated |
| Funding as a feature on the drawdown head | **−0.0005** AUC. The item the ML document called "most likely real improvement available" is closed |
| `runs/features/forecast.py`, a model-driven `exposure_scale` clamp, `{{FORECAST}}` in the prompt, the shadow arm, the console page | All struck. There is nothing to write that trailing volatility does not already say; every time-series form loses to holding; a 0.007-AUC increment does not deserve a prompt slot; there is nothing left to shadow |
| A model-based de-risk on BTC/ETH | Every rule loses to holding on both assets; **3 of 8** make the drawdown worse, because it de-risks into volatility and misses the rebound |
| A repo-wide hunt for the first-bar lookahead | **Corrected by two reviewers.** The ~1.3 Sharpe inflation was real but confined to two throwaway audit scripts; there are **0 instances** in `strategies/` or any repo backtest harness, and the measured effect on real Binance bars is **≤0.07 Sharpe**. Apply the lesson to future audit scripts; do not spend an afternoon searching the repo |
| Momentum or rally-picking selection | Cross-sectional momentum rank IC −0.016 to −0.069; a costed top-8 rotation is **−13.5%** a year at **−98.9%** drawdown, losing *before* costs; only 3.4% of coins beat BTC in 2023-24; **77.5%** of +100% run-ups are given back within 60 days |
| Maker-only execution, entry timing, order slicing | 12 hypotheses refuted. Only BNB fee payment (+0.215% of NAV a year) and USDT cash yield (~+0.5 points a year per 1% APR) survived, and both as arithmetic, not alpha |
| Heavy analysis against the live databases while the bots trade | Today's 6h42m blocked-entry window, with `database is locked` on three healthchecks and no ingest rows from 07:00 to 13:41 UTC, coincided with exactly that. Snapshot copies only, handed over under the ops lock |

**A hard ceiling worth restating, because several of the above keep colliding with it:** a
long-only spot book that is already fully invested **cannot express upward conviction**. Every
signal it can act on is expressible only as a **reduction** from fully invested. Any plan that
assumes "a better forecast will raise returns" is arguing against that proof and should say so
out loud.

---

## 9. Honest limits

**What none of today's work touched, and it is the biggest gap.** All six tracks held the exit
side fixed. The live evidence says the exit is where this system loses (10-of-10 versus
0-of-8), and a study that fixes the exit cannot speak to it. Build item 1 rests on the ledger
and on live fills, **not** on anything measured today.

**No live data, anywhere.** Not one of the six tracks opened `knowledge/earn.db`,
`journal/journal.db` or any `ft_userdata` file, by rule. So nothing here is reconciled against
real fills; the live exit numbers are **cited**, not re-derived; the realised rate at which the
staleness gate has blocked entries is **unmeasured**; and three of the forensics loop's named
causes (screener drop, validation expiry, gate refusal) are defined but not populated. SleeveB
— the Claude-proposed sleeve — is not replayed anywhere in this document. Only SleeveA is.

**Nothing here clears the honesty hurdle, and nothing is offered as an edge.** Cumulative
selection trials went from ~8,144 to **8,805** across the six tracks (+313, +225, +26, +76,
+12, +9, each tallied by its own track; each track quoted its own cumulative against 8,144, so
this is the first place the six are summed). The deflated hurdle sits at **2.32 to 2.98** depending on
the window, against a BTC-hold baseline of 0.83. The best number produced anywhere today is
**1.028** (a volatility overlay against a base book of 0.820) — and that same overlay does not
beat BTC hold significantly (t +0.80) and has a **5-point worse drawdown** than BTC. Every item
in section 7 is a defect fix, a tail guard, or an observability fix. Not one is an edge.

**Where a reviewer corrected a number.** Six places, and each one matters:

| what was claimed | what verification found |
|---|---|
| The first-bar lookahead should be hunted through `strategies/` — it inflated this rule family by ~1.3 Sharpe | Two reviewers: **0 instances** in `strategies/` or any repo harness; the real effect on Binance bars is **≤0.07** Sharpe. The 1.3 was real, and confined to two throwaway audit scripts |
| The drop-only risk model partly re-learns the eligibility filter (age +3.13, adv90 −1.09, amihud −1.17) | Those are the **two-sided** model's coefficients, not the drop-only model's, which are age −0.492, adv90 +0.055, amihud +0.357. Refitting without all three gives AUC **0.618** against 0.634 — the caveat was roughly sixfold overstated |
| The test universe matches the live one (17.2 reconstructed against 16 live) | The **count** matches (16.2 on an independent rebuild) but the **composition** does not: only **14 of the 16** live names reproduce. ZEC and NEAR are missing because the quality filter is applied to satellites only, never to majors — and ZEC is the largest 2026 riser in the book |
| H2 and H3 are "inconclusive" only because the audit refuses a non-Sharpe headline | The gate has **no such rule**. Both were recorded inconclusive by the study itself with no headline supplied, so the gate was never asked to rule. The measurements stand; the explanation was a reporting defect |
| Every new-feature result carries survivorship bias, because klines serves only listed symbols | Klines **does** serve delisted symbols (30 of 30 sampled returned real history; delisted pairs are also enumerable as status `BREAK`), and 493 of 503 live pairs are in the panel, not 468. Those results are near-survivorship-free. One residual hazard stands: reused tickers (`LUNAUSDT` is LUNA 2.0 today) splice two different assets under one symbol |
| `CLAUDE.md` describes a 17-check risk gate | The code's `CHECK_ORDER` has **27** entries today. `CLAUDE.md` is stale on this, and section 5's "none of them is a venue check" is stated against 27 |

**Smaller limits, stated because they bound specific rows above.** The wide-universe study is
nine folds and daily, not hourly, so its magnitude is imprecise and it could not test the
shipped rule's exact intraday cadence. One volume finding rests on a proxy, not on real
taker-buy flow (build item 10). The cadence study's 31-pair sample is today's whitelist and
therefore survivorship-selected, which inflates every hold baseline in it (though not the paired
comparisons that carry its findings), and its median-CAGR figures are only meaningful to about
±2 points. The ML re-run had four purged folds, not five, and its weakest fold trains on 4,585
rows. The forensics loop is daily while the sleeve trades 4-hour bars, so intraday rallies are
invisible, and it replays SleeveA only. The attention study's cross-section is 16 hand-mapped
survivors, which biases *in favour* of the hypothesis it refuted.

**Two of the six tracks disclosed their own errors, and both were caught by cross-checking
rather than luck** — a return-grid artefact that produced a beautiful and false "every hour of
lag costs a point" curve, and a look-ahead leak that made looking *less* often reach Sharpe
2.17. Both are reported in their files and both are counted in the trial totals above. A version
of this work that shipped either would have been wrong in a way you could not have checked.

**One housekeeping note.** The six track reports live under `evals/research/improve/`, which is
a tier-2 (human-only) path. Nothing was committed and no code was changed, but a human may want
them moved to `reports/`. This document is under `docs/design/`, which is in neither the tier-2
nor the tier-0 list. A stray workspace copy at `momentum/im5/` should be checked and removed;
its sibling at `momentum/im3/` was already cleaned up.
