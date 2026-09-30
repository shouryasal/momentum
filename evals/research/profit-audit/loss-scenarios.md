# Where will we lose? The loss-scenario register

**Measure 2 of the profit audit — 2026-09-30 — workspace `pa2`**

Scripts: `evals/research/profit-audit/loss_m1.py`, `loss_m2.py` (read-only; panel and live
data opened `mode=ro`). Nothing in this document re-derives a number the rejection ledger
already settled; where a figure exists in `docs/design/*`, it is cited and reused.

**Selection trials added: 0.** Nine measurement families were run. Not one of them searched a
parameter, chose a threshold or picked a winner from a set — each is a single description of
data already on disk. The cumulative trial count stays at ~8,134 and the deflated hurdle
stays at ~2.32.

**Cost floor applied throughout: 0.30% round trip** (10 bps fee + 5 bps slippage per side).
Every percentage below is net.

---

## 0. The answer in one paragraph

The system's biggest loss is not a market event. **It is our own stop firing into a wick that
undoes itself, and our own host being asleep when a stop is breached.** Of the 12,910 days
across the 31 live pairs where the price dug 6% or more below the prior close — the depth of
our own stop — **57.4% closed back above that line the same day**. On BTC alone that is 175
of 300 such days. Each of those is a sale at the worst price of the day followed by a
recovery: 0.30% of cost paid plus a median 2.39% of recovery handed away, 2.69% of the
position, and the stop has no confirmation and no latch anywhere in the code. Second, the
host was awake for about 29 of the first 132 hours of paper trading, and when a 6% stop is
breached and nobody is there, BTC finishes a 14-hour absence a **median 1.46% and a mean
2.00% below the stop price** (n=3,925), with a 1-in-100 case at **−21.21%** and a worst case
at **−43.82%**. Third, and the only good news of comparable size: being spot-only avoids
**11.56% a year** of funding that an always-long BTC perpetual paid, **81.4% of notional over
seven years**, and the liquidation risk that comes with it. That is the largest single
measured structural advantage the book has, and it is arithmetic, not skill.

Two scenarios turn out to be nothing, and should stop being worried about: there is no
weekend in crypto, so there is no weekend gap; and the freshness clock cannot be frozen
fresh, because it records the age of the *data*, not the time of the *write*.

One scenario turns out to be the book winning: the 422 days above the BTC cap are, in the
median, days the book was up. Trimming them is expected-negative in the middle and only
protective in the tail — which is why the order-path reviewer's refusal of the trim was right.

---

## 1. The register, ranked by expected cost

Cost is expressed as a share of NAV. "Detected today" names the line that would see it, or
says nothing does.

| # | Scenario | What happens | Detected today (file:line) | Measured / bounded cost | What would have to be true for it to be covered |
|---|---|---|---|---|---|
| **1** | **Stop fires into a wick that recovers** | The 6% fixed stop (`risk.stoploss_per_trade` 0.15, sleeve `stoploss.fixed_pct` 0.06) or the trailing stop (activate +1.5%, distance 0.8%) is touched by the day's low and the price closes back above it. We sold the low. | **Nothing.** `strategies/mechanics.py:36` lists `stoploss` in `ACTION_PRIORITY` with no confirmation bar and no latch; `riskgate.check_exit` (`strategies/riskgate.py:1062`) allows every exit unconditionally. | **20.31% of all whitelist bars** had a low ≥6% under the prior close (12,910 of 63,566). **57.4% of those closed back above the line** (7,409). Median recovery forgone **2.39%**; plus 0.30% cost = **2.69% of the position per whipsaw**. BTC: **175 of 300** such days are whipsaws, ≈19 a year. Upper bound at a 40% BTC weight: **>8% of NAV a year**. | A stop that requires an N-bar close below the line (not a wick), plus a latch so one breach cannot re-fire, plus a re-entry rule that is not just the 2h `reentry_cooldown_hours`. |
| **2** | **The host is asleep while a stop is breached** | The laptop hibernates. Cron does not run, the containers do not run, no stop executes. We sell when it wakes. | **Nothing on the host can see this** — `ops/healthcheck.py` runs every 5 minutes from cron, and cron is asleep too. Documented after the fact in `docs/design/outage-2026-09-25.md`. | Measured uptime: **29 of the first 132 hours (22%)**. After a 6% breach, a **14h** absence ends BTC a median **−1.46%** / mean **−2.00%** below the stop, p5 **−11.01%**, p1 **−21.21%**, worst **−43.82%**; **65.6% of breaches end worse than the stop**. ETH 14h: median −1.64%, mean −2.55%, p1 −22.60%, worst −47.68%. The measured 63h outage: worst unconditional BTC move **−48.60%**, ETH **−52.53%**. | A watchdog *off the host* — a cheap external ping that notices the heartbeat stopped. Nothing inside a sleeping machine can raise an alarm. |
| **3** | **Correlated drawdown across all 31 pairs** | Everything falls together and the 31-name book behaves like one position. | `riskgate.py:1005` (`corr_cap`) and `:1004` (`beta_cap`) — but both are measured **only on the book an incoming entry would create**, never on the book already held, and the cap is 0.70. | Average pairwise correlation: **0.526** whole-sample, **0.588 on the worst 5% of BTC days**, **0.343 on the best 5%**. The cap of 0.70 **is never reached in a crash**, so `corr_cap` cannot fire when it matters. Equal-weight 31-pair book at 80% gross: worst 1-day **−33.53%** (2020-03-12), worst 3-day **−34.70%**, worst 30-day **−47.08%**, max drawdown **−80.03%**. **439 of 3,324 days (13.2%)** trip the −3% daily stop. | A correlation measure taken on the *held* book each tick, and a cap set where the crash regime actually sits (0.60), not above it. Diversification here is asymmetric the wrong way: 0.34 when you want it, 0.59 when you need it. |
| **4** | **Cap breach with no order for the gate to refuse** | A position grows past its cap because the price rose. No order is raised, so the gate never sees it. | `strategies/mechanics.py:88` `trim_reason` classifies exactly this — but the ensemble TRIM that would act on it is **built and not deployed** (77 tests, refused by the order-path reviewer). The gate refuses orders only. | Cited: **422 of 3,326 days above the BTC cap**, 136 above gross (`exit-and-horizon-2026-09-29.md` §2). My unrebalanced bound: 95.7% of days, median excess **+43pp**. **Priced: excess weight × forward 30-day BTC return is +1.16% of NAV on average** — the breach is expected-**positive** — with a worst single breach day of **−21.72% of NAV**. | Nothing, in the median. This row is correctly accepted: trimming a winner costs return in the middle and only pays in the tail, and the reviewer's four objections (no latch, fee-budget-silenceable, a resting buy can fake a breach, SleeveA could sell under KILL) are all still true. |
| **5** | **A coin is delisted while we hold it** | Binance announces, volume evaporates, the price collapses, and we are still in. | `ops/universe.py:454` checks `status != "TRADING"` — but it runs only inside `ops/universe_refresh.py`, and **there is no cron job for it**: the 17 installed jobs are ingest, scanner, signals, nav_tick, reconcile, healthcheck, tca, nav, research ×2, daily_review, discovery ×2, review, backtest_data, backup, maintenance. `exit_only` is empty in `config/riskgate.json` and nothing writes it automatically. | Panel: **250 of 747 tickers are dead**. Final 30 days: median **−41.5%**, and **65.7% lost >20%**, **42.4% lost >50%**. Final 7 days: median **−14.8%**. Worst 7-day window inside the final 30: median **−37.7%**, p10 **−77.0%**. Restricted to names that were liquid (≥$10M/day) — the ones we could actually hold — median final-7d **−4.6%**, median final-30d **−24.7%**, median worst-7d **−32.1%**. Bounded by the satellite cap: 2 seats × 2.5% ⇒ **max realistic −1.9% of NAV**, absolute worst −5%. | A scheduled `universe_refresh` (weekly is enough) plus an automatic `exit_only` write on a `status != TRADING` symbol, and an exit — not just a block on entry. Today `exit_only` stops buying and sells nothing. |
| **6** | **USDT depegs** | The unit every price, every cap and every cash balance is quoted in stops being a dollar. | **Nothing. No scheduled job checks the peg at any point.** The `venue-guard` skill's `venue_state.py` exists and no code in `ops/` or `runs/` calls it. This is the single clean hole in the register. | Worst daily closes on Binance's own stablecoin pairs: **USDC 0.9592** (2023-03-11, −4.08%), **TUSD 0.9598**, **USDP 0.9402 intraday** (−5.98%), FDUSD 0.9860. Cash is 20–60% of NAV (`usdt_floor` 0.20, `max_gross_exposure` 0.80). A −4% print on a half-cash book is **−2.0% of NAV**, and simultaneously every USDT-quoted price the gate and the stops read becomes wrong by the same amount. A −10% depeg: **−5% of NAV** plus a book priced off a fiction. | One cron job calling the script that is already written, and a flag (`ops/lib/flags.py:128` `set_flag`, severity `block_entries`) on a deviation past a threshold. This is the cheapest row in the table to close. |
| **7** | **The daily −3% stop locks entries while the book keeps falling** | `risk.daily_loss_response = hold`: sell nothing, buy nothing for 24h (`riskgate.py:1125-1133`). The book rides the fall. | Yes, and deliberately — `riskgate.py:70-77` and `crisis-policy.md` §0. | Fires on **438 of 3,317 days (13.2%)**, ≈48 a year. The forward return **after** a trigger is *better* than average: **+0.67% median 1-day vs +0.17%** on other days (mean +0.64% vs +0.07%); BTC +0.47% vs +0.15%. So the 24h lock forgoes the above-average days. Upper bound on forgone dip-buying: 20% max order × 0.64% × 48 = **6.1% of NAV a year**. | Correctly accepted as a *response* — `crisis-policy.md` §0 measures hold at **+30.53% CAGR** against halve +13.05% and flatten −5.23%. What is *not* settled is the 24-hour width of the lock, which this is the first measurement of. |
| **8** | **The kill switch is engaged with positions open** | It does not flatten. | Verified: `riskgate.py:942` blocks entries, `earn_base.py:1040` cancels resting entry orders, `riskgate.py:1062` allows every exit, and `flatten_pending` (`riskgate.py:1214`) **never reads the kill file**. KILL = stop buying, keep stops armed. | The book keeps its full gross for as long as the human takes. Over a 63-hour response window — the length of the outage we actually had — the worst measured moves are BTC **−48.60%**, ETH **−52.53%**, and the 31-pair book's worst 3-day is **−34.70%**. | Correctly accepted, but the owner must know it: the red button stops buying, it does not sell. Given flatten measures at −5.23% CAGR, a kill that sold would be worse on average. The stops are what protect the book under KILL, which makes row 1 the real dependency. |
| **9** | **The model tier fails silently behind `on_all_failed`** | Every model in the chain fails; the task's fallback answers instead of a person. | `config/models.yaml:342` `decide: on_all_failed: hold_last`; `:322` `discover: abstain`; `:281` `validate: drop_signal`; `:132`/`:156` `extract`/`classify`: **`rule`** — keyword rules answer in a model's place. `proposal.max_age_hours = 48` then ages the held proposal out. | **Already paid, live.** Sleeve B took 2 `target_zero` exits on 2026-09-23: **−27.56 USDT of which 15.01 was fees** on a 10,000 pot = **0.15% of NAV in fees for one plumbing round trip**. The phantom flatten that caused it (F2) **is fixed in the working copy** (`strategies/SleeveB.py:391-412`), so the register downgrades it; `SL-09` (`runs/router.py:304`, `two_abstains` self-perpetuating, 6 of 6 decides escalated to Fable@max) is still open and burns budget rather than NAV. | An alarm when a task's answer came from `on_all_failed` rather than a model, distinguished on the console. A silent `rule` answer that looks like a model answer is the failure mode, not the fallback itself. |
| **10** | **A flash crash or fat-finger wick** | A momentary print far below the market. | Nothing rejects an outlier print. Entries are `market_entries_allowed = False`, which helps on the buy side only. | BTC wick depth below the candle body: p50 **−1.01%**, p5 **−4.40%**, p1 **−8.87%**, worst **−21.21%**. Across the whitelist: p1 **−12.18%**, worst **−99.99%** (a bad print in the panel itself — evidence that outlier prints exist in the data we price off). This is the same instrument as row 1; the wick is how the stop gets hit. | Same fix as row 1. A stop that needs a *close* below the line is immune to a wick by construction. |
| **11** | **A single name blows up inside the 5% satellite sleeve** | One satellite goes to nearly zero. | `riskgate.py:994-997` — `satellite_count` ≤ 2 and `satellite_gross` ≤ 5%; `cap_stake` (`:1046`) trims rather than refuses. | Liquid names (≥$10M/day, 79 names, 115,394 name-days): p1 daily **−14.08%**, p0.1 **−27.46%**, worst single day **−90.45%** (EOSBULL, 2020-03-12), worst 7-day **−95.55%**, p1 7-day **−32.09%**. A name fell >30% in a day on **0.081% of name-days**. At 2 seats × 2.5% of NAV: a bad seat is **−0.8% of NAV**, a total loss of both seats is **−5.0%**. | **Correctly accepted.** The cap *is* the control, and 5% is the honest price of the whole satellite experiment. Long-only spot cannot lose more than the position. |
| **12** | **An exchange API outage** | Binance stops answering. | Yes, and it works. The gate's `staleness` check (`riskgate.py:948`, `risk.staleness_minutes = 30`) fails closed on a stale book snapshot; `ops/lib/freshness.py` `BLOCKING_SOURCES` makes candles and books blocking and news/funding advisory. | **Measured live, 7 days of log, sleeve A:** 147 `Could not load markets` (ExchangeNotAvailable), 618 order-book `NetworkError`s, 742 candle-fetch failures each for BTC and ETH, 1,217 `NetworkError` and 998 `Timeout` in total. Sleeve B is near-identical. **Zero orders refused, zero gate refusals.** The API is unreliable all the time and the fail-closed design absorbed it. | **Correctly accepted.** The 30-minute staleness window is the control and it held through ~3,500 transport errors in a week. |
| **13** | **An ingest outage freezes the freshness clock** | The sidecar keeps saying "fresh" while no data arrives. | **Cannot happen.** `ops/lib/freshness.py` records *the newest timestamp of each source*, not the time of writing, and returns `inf` — which every caller reads as stale — when the file is missing, unreadable, malformed, or carries no blocking source at all. `runs/ingest.py:73` makes only `candles` and `books` fatal. | **Nil.** This row is closed by construction. Verified against the writer's own docstring and `riskgate.data_age_minutes` (`riskgate.py:639`). | Nothing. Worth recording as *verified safe* so it stops being a worry. |
| **14** | **The trend file goes stale and the entry gate fails closed** | `knowledge/state/trend.json` stops being written; the ensemble gate refuses every core entry. | Yes, by design. `strategies/trend_state.py:42` `DEFAULT_MAX_AGE_HOURS = 48.0`, judged on the **bar** not the file (`:139-141`), returning `trend_state_stale` and weight 0.0. Journalled once per pair per side per state change (`earn_base.py:_trend_weight_for_trim`). | **How long to stop all trading: 48 to 72 hours** after the last daily close, and it stops only *core*. A satellite is never gated (`earn_base.py` `_trend_weight` returns 1.0 / `not_core`), so BTC and ETH — 70% of the book's capacity — freeze while the 5% satellite sleeve keeps buying. Cost is opportunity only; no position is sold, because the plumbing reasons are excluded from `_SELLABLE_TREND_REASONS`. | Correctly accepted. The one thing worth noting is the asymmetry: a dead writer stops the good half of the book and leaves the speculative half running. |
| **15** | **A venue halt or a withdrawal freeze mid-position** | Binance suspends the symbol or freezes withdrawals. We cannot get out. | **Nothing scheduled.** Same hole as row 6 — `venue_state.py` is written and never called. `ops/universe.py:454` would catch a non-`TRADING` status if anything ran it. | Unbounded in duration; bounded in mechanism, because spot-only means **no liquidation and no margin call** — the loss is only the price move we cannot exit through. Using the measured 63h window: up to **−48.60%** on the BTC leg. | The same single cron job as row 6. One job closes rows 5, 6 and 15. |
| **16** | **A stablecoin-pair liquidity hole** | The quote side thins out and our order moves the price. | `riskgate.py:967` `min_notional`, `:969` `step_size` against the symbol's own filters, `:970` `max_order_notional_pct` = 0.20. | Not a cost — a **governance breach**. Our largest single order (2,000 USDT of a 10,000 wallet) is **0.038% of the thinnest name's daily volume**, so slippage is a non-issue. But **10 of the 31 live pairs sit below the $10M/day median volume that `growth-audit.md`'s own exclusion filter requires**: DOT $5.26M, FIL $5.35M, INJ $5.36M, HBAR $5.40M, PENGU $5.49M, BCH $6.81M, plus FET, TRUMP, ONDO, XPL. BTC is $1,069M. | The whitelist being generated by the filter the research specified, rather than by hand. The filter exists in a document and not in the config that produced `pair_whitelist`. |
| **17** | **A funding or basis shock** | Perpetual funding spikes and longs bleed. | Not applicable — spot only. | **What we are NOT exposed to, measured.** An always-long BTC perpetual paid **11.56% a year** (mean interval 0.01056%, 3×/day), **81.4% of notional over 2019-09 → 2026-09**, worst rolling year **+33.56%**, worst 30 days **+7.71%**, and **paid on 85.8% of all intervals**. ETH **13.79%/yr** (94.2% total), XRP **14.60%/yr**, LINK **13.76%/yr**, DOGE **12.39%/yr**, AVAX **6.86%/yr**. | Nothing. This is the register's one large positive and it should be said out loud: **the decision to be spot-only is worth about 11–15% a year against the leveraged version of the same book, before counting the liquidations.** |
| **18** | **A gap through the stop over a weekend** | — | — | **Nil. There is no weekend in crypto.** Binance spot runs continuously; the panel has a bar for every calendar day. The only gap that exists is our own downtime, which is row 2. | Nothing. Recorded so the worry can be retired. |
| **19** | **The monthly −10% stop is the only thing that flattens** | One −10% Gulf-calendar month sells the entire sleeve at market. | `riskgate.py:1119-1124` returns `LoopActions(flatten=True, flatten_reason="risk_stop_monthly")`. The lock now releases at the month boundary (`_monthly_lock_active`, `:1138`) after a 2021-05-22 backtest once ended a run permanently. | The **only automatic full flatten in the system is the response `crisis-policy.md` §0 measures as the worst of the three** (−5.23% CAGR vs +30.53% for hold). It sells the whole sleeve, crossing the spread, at the point of maximum drawdown. Frequency: the 31-pair book's worst 30-day is −47.08%, so a −10% month is not rare. | Either the monthly response becomes `hold` like the daily one, or the difference is argued from a measurement. Today the daily stop uses the best-measured response and the monthly stop uses the worst, and nothing explains why. |
| **20** | **Fees eat a small book** | Round trips cost more than the trades make. | `riskgate.py:958` `fee_budget` (`max_fee_pct_per_month` = 0.01) and `:964` `min_edge` (3.0 × 0.30% = a **0.90% required booked target**). | **The live evidence is four trades.** Sleeve A: 2 `force_exit`, −42.21 USDT. Sleeve B: 2 `target_zero`, −27.56 USDT. Combined **−69.77 USDT with 19.92 of it fees — 29% of the total loss was fees**, on a 20,000 simulated pot. Cited: the 1h rule earns **+0.022% per trade against the 0.300% it costs** (`exit-and-horizon-2026-09-29.md`). | Fewer trades, or a real edge. `min_edge` at 0.90% is the right shape of control; it is the per-trade edge of +0.022% that is the problem, and that is Measure 1's subject, not this register's. |

---

## 2. The three worth building against

**1. A stop that cannot be whipsawed (rows 1 and 10).** This is the largest expected cost in
the register and the only one whose fix is entirely inside our own code. 57.4% of 6% breaches
across the whitelist, and 175 of 300 on BTC, close back above the line the same day, at
2.69% of the position each. The fix is three things the order-path reviewer already named
when it refused the trim: require a **close** below the line rather than a wick, **latch** the
breach so it cannot re-fire, and make the re-entry rule something better than a 2-hour
cooldown. Note that the same reviewer's objection applies to the current stop too — it has no
latch either.

**2. One cron job that checks the venue and the peg (rows 6, 5 and 15).** `venue_state.py` is
already written and nothing calls it. The crontab has 17 jobs and none is this one. It is the
only hole in the register where **nothing at all** runs, and it closes three rows at once: a
USDT depeg (−2.0% of NAV on a 4% print against the half-cash book, and every price in the
system wrong at the same time), a delisting while held (median −24.7% over a liquid dead
name's final 30 days, bounded at −1.9% of NAV by the satellite cap), and a venue halt. This
is the cheapest row-per-hour in the table.

**3. A watchdog that lives off the host (row 2).** The host being asleep is the second-largest
measured cost — a mean 2.00% below the stop on a 14-hour absence, a 1-in-100 case of −21.21%,
and 22% measured uptime — and **nothing on the host can detect it**, because cron is asleep
too. This does not need to be clever; it needs to be elsewhere.

## 3. What is correctly accepted, and why

- **The kill switch does not flatten** (row 8). Deliberate, and better on average than the
  alternative: flatten measures at −5.23% CAGR against +30.53% for holding. The owner should
  simply know that the red button stops buying and does not sell.
- **The daily stop holds rather than sells** (row 7). The best of three measured responses.
  Only the 24-hour *width* of the lock is unargued, and this is its first measurement.
- **The 5% / 2-seat satellite cap** (row 11). The cap is the control; −5% of NAV is the
  honest, bounded price of the whole satellite experiment.
- **Spot-only** (row 17). Worth 11–15% a year against the leveraged version, plus no
  liquidation. The single best structural decision in the system.
- **The 30-minute staleness fail-closed** (row 12). It absorbed ~3,500 transport errors in
  one week without refusing a single legitimate order.
- **The freshness clock** (row 13) and **the weekend gap** (row 18) are closed by
  construction. Retire both worries.
- **The 422 cap-breach days** (row 4). Expected-**positive** (+1.16% of NAV on average),
  tail-negative (−21.72% worst). The trim's refusal was right on the merits.

## 4. The one row that deserves a second look but is not in the top three

**Row 19 — the monthly stop.** The daily stop uses the best-measured response (`hold`) and
the monthly stop uses the worst (`flatten`), and no document explains the asymmetry. This is
not ranked in the top three because it has never fired in paper and its frequency is
unmeasured on the deployed book; it is ranked here because the inconsistency is free to notice
and cheap to argue.

## 5. Honest limits of this register

- **Four live trades.** Everything about the deployed book's *behaviour* is inferred from the
  panel and from the code, not from live evidence. Sleeve A's 2 `force_exit`s and Sleeve B's 2
  `target_zero`s are the entire live record, and the −69.77 USDT they lost is not a sample.
- **The whipsaw cost in row 1 is an upper bound.** It counts every day where the price dug 6%
  below the prior close, not every day where we actually held a position with a stop 6% under
  its entry. The per-event figure (2.69%) is solid; the events-per-year figure assumes a
  position is open, and four trades cannot tell us how often that is true.
- **Row 3's book is equal-weight across 31 pairs at 80% gross.** The real book is capped at
  BTC 40 / ETH 30 / satellites 5, so its worst 1-day is smaller than −33.53%. The
  **correlation** finding (0.588 in crashes, 0.343 in rallies, against a 0.70 cap) does not
  depend on the weighting and is the part that matters.
- **Row 4's 95.7% is a looser construction than the cited 422 of 3,326.** Mine never
  rebalances; the deployed book trims through its MA200 flip and its stop. The cited 12.7% is
  the authority. My figure only shows that the drift is structural.
- **Row 6's depeg magnitudes are proxies.** USDT's own price is not in a USDT-quoted panel, so
  the worst observed deviations of USDC, TUSD, USDP and FDUSD stand in for it. The panel also
  contains bad prints (a 0.20 low on USDC, a −99.99% wick on a whitelist name), so only daily
  closes were used for the peg figures.
- **Row 17's funding is measured on Binance perpetuals from 2019-09.** It prices the
  counterfactual, not our book. No position was ever exposed to it.
- **21 of the 31 pairs have at least half the panel's history.** Ten are too young for the
  correlation and drawdown work, so rows 3 and 16 speak for the 21 that qualify.
- **Nothing here was walked forward,** because nothing here is a forecast. Every row is a
  description of what happened or a bound on what could. No row should be read as a claim
  about a future rate.
