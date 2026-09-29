# Paper-trading review, 2026-09-23 → 2026-09-29: what we are doing wrong

Written 2026-09-29 05:35Z (09:35 Gulf) for the owner, who asked "what are we doing wrong".
Everything here was read from copies of the journal (`journal/journal.db`), the knowledge
database (`knowledge/earn.db`) and the four bot databases, opened read-only, by two
measurement passes (tr1: trades, costs, ladder, benchmarks; tr2: the gate, the signal funnel,
the timing). The design documents are cited, not re-measured. No code was written, nothing
under `~/earn-run` was touched, no bot, order, cron job or setting was changed.

Plain words for three terms used throughout. **The gate** is the automatic checklist every
order must pass before a bot may send it (26 checks). **The fast-test profile** is the
short-horizon configuration both bots have been running since 09-23 23:37Z: 1-hour candles,
an EMA(6/18) breakout entry, profit rungs at +0.6% and +1.2%, a 6% stop. **Sleeve A / Sleeve B**
are the two paper bots, 10,000 simulated USDT each; under this profile both run identical
rules, so Sleeve B's normal job (trading the model's proposals) was parked.

---

## 0. The answer

**The 20,000 is not worth less because of the strategy, and not because of the market.** By
the console's own ledger it is worth **20,009.68 USDT** (+9.68), with nothing open. Counting
the two events of the first evening that the console's midnight reset hid, it is
**19,939.91** (−60.09, −0.30%). Of that 60.09, **42.21 was a hand-typed "SELL EVERYTHING"** on
the console at 21:58Z on 09-23 (01:58 Gulf), and **27.56 was Sleeve B buying double what it
was told to and selling everything fifteen minutes later** when it found it had no valid
mandate; both happened on day one, before the strategy under test had placed a single order.
The rules that actually traded made **+19.69 before fees and +9.68 after** on ten trades, and
the market went **up** while they did so (BTC +0.52%, the 31-coin basket +9.2% over the trading
window). The honest problem is different: **the system barely ran.** The laptop lid was
closed and the machine slept for 14 hours on 09-24 and for 64 hours from 09-25 12:16Z; the
bots were awake for about **29 of the 132 hours** since the test began. When awake they ran
a profile whose own header calls it "a PLUMBING TEST, not an edge test" and which **loses
1.3% a month** in its own costed backtest; the model path produced **zero usable decisions**
(7 of 7 proposals abstained on empty inputs, 15 of 15 validations errored, about 24 USD
spent). The owner's arithmetic on costs is right: a +0.6% profit rung hands **half** its gain
to a 0.30% round trip, and **51% of this week's gross profit went to fees.** But the rungs are
not where the money went — every alternative rung set replayed moves the result by under
3 USDT — the **holding horizon** is: sub-day holds are the only holding period the studies
find significantly negative. What this implies: the next test must run on a machine that
stays on, must run the strategy we intend to ship (the BTC/ETH trend ensemble, built this
morning but not yet wired in) rather than the plumbing profile, must show the cumulative pot,
and must be judged for 30 days on whether the mechanism behaves — not on return, which 30
days cannot measure.

---

## 1. What the 20,000 is worth, and why

### 1.1 Three definitions, one answer each (as of 2026-09-29 05:30Z, no open positions)

| definition | Sleeve A | Sleeve B | total | vs 20,000 |
|---|---|---|---|---|
| **What the console shows**: the fast-profile ledger, closed trades net of fees | 10,005.31 | 10,004.37 | **20,009.68** | **+9.68 (+0.048%)** |
| Same, before fees | 10,010.32 | 10,009.38 | 20,019.69 | +19.69 |
| **Cumulative, including the first evening's two events** | 9,963.10 | 9,976.81 | **19,939.91** | **−60.09 (−0.300%)** |
| If both sleeves had simply held BTC from 09-23 17:00Z, valued now | — | — | 19,773.96 | −1.13% |
| If both had held the equal-weight 31-coin basket, valued now | — | — | 21,139.98 | +5.7% |

The console reset its ledger to 10,000 per sleeve at 09-23 23:45Z when the fast-profile
databases started, so it forgot the two evening losses. That is why the owner remembers
20,000 going in and the console says 20,009.68: **both numbers are correct, they count from
different starting lines.** The number to hold in mind is 19,939.91: down 60 USDT, 0.3%.

### 1.2 Where the 60.09 went

| bucket | USDT | % of 20,000 | whose |
|---|---|---|---|
| **Market move** — what holding BTC did over the trading window (09-23 17:00Z → 09-25 12:15Z), costed | **+36.54 per 10,000** (BTC 84,038 → 84,583, +0.52% price-only). The 31-coin basket did +9.2%. | not a loss | market |
| **Operator action** — "SELL EVERYTHING" typed on the console, Sleeve A, 09-23 21:58Z | **−42.21** (price −37.30, fees −4.91) | −0.21% | operator |
| **Plumbing** — Sleeve B bought BTC+ETH twice within 68 ms (a race between the proposal order and a rebalance top-up placed before the first fill was counted), ending at 75% of NAV instead of the 25% asked; 15 minutes later it found "no proposal ever" as its mandate and sold everything | **−27.56** (price −12.55, **fees −15.01** on 15,006 USDT of turnover) | −0.14% | code |
| **Strategy** — the fast profile, 10 trades, gross | **+19.69** | +0.10% | strategy |
| **Execution cost** on those 10 trades (dry run charges 10 bps per side, no slippage) | **−10.01** (51% of gross). At the real 0.30% round trip the same ten trades net about +4.7 | −0.05% | cost |
| **Not trading at all** — awake 28.6 of 43.25 hours in the window (66%); dark 64h36m from 09-25 12:16Z | **0 realised.** Replays bound what was forgone at +30 to +192 USDT under optimistic fills, and the same rule loses 1.29% per 30 days in its own backtest, so no honest number can be put on it | — | infrastructure |
| **Total** | **−60.09** | **−0.30%** | |

Three sentences the owner should take from this table.

1. **The market did not do this.** BTC rose half a percent across the window and the wide
   basket rose nine. A book that simply held either would be ahead; this one is flat.
2. **116% of the loss is the two day-one events** (−69.77 of −60.09). Neither is the
   strategy. The hand flatten was done at the console twenty minutes after the first
   stale-data alarms of the evening (`ops_incidents` 1–3 at 21:40Z), and it is the same
   instinct `crisis-policy.md` measured as the single worst policy in the system: flatten
   on a loss trigger = −5.23% a year, halve at the same trigger = +13.05% (§0, rows for the
   shipped daily stop). The record says nothing about why; the bot was restarted at 22:17Z.
3. **The strategy under test made money this week, 6 wins in 10, and that means nothing.**
   Its own 30-day backtest, costs on, is 92 trades and −1.29%, with fees at 1.35% of the
   balance — the fees *were* the loss (`config/profiles/fast-test.yaml`, evidence block;
   `analogue-timing.md` §4.4 reproduces it: 90.9 bps of fees per holding day, −14.6%
   annualised). A good week in a rule that loses on average is noise, exactly as a bad
   week would be (`dip-strategy.md` §10.3).

### 1.3 The ten trades that did happen (both sleeves; net reconciles to the bots' own ledgers)

| sleeve | pair | entered (UTC) | held | exit | net USDT | return |
|---|---|---|---|---|---|---|
| a, b | TRX | 09-24 01:05 | 19.5 h (the host slept 14 of them with it open) | trend-loss signal | −5.22 each | −1.04% |
| a, b | NEAR | 09-24 20:45 (9 min after the machine woke) | 9 min | rungs + trailing stop | +4.31 / +4.34 | +0.86% |
| a, b | BTC | 09-25 01:45 | 4.3 h | trend-loss signal | −3.00 each | −0.60% |
| a, b | ONDO | 09-25 07:00 | 7 min | rungs + trailing stop | +4.78 / +4.72 | +0.95% |
| a | INJ | 09-25 12:00 | 14 min | rungs + trailing stop | +4.44 | +0.89% |
| b | SOL | 09-25 12:00 | 12.1 h (across the sleep; exit filled during an 80-second wake-up on 09-26 00:07Z) | ROI table (+0.5% after 5 h) | +3.54 | +0.71% |

Every position was 5% of NAV (about 500 USDT), because the gate's satellite cap clamped every
one of 2,007 sizing requests to that ceiling. **A +0.9% trade on 500 USDT is +4.5 USDT on a
10,000 wallet by construction.** The three rung winners left names that went on to move +7%
(INJ, then −7.7% within a day), +17% (NEAR) and +32% (ONDO) over the window; the two losers
were both the trend-loss exit, which the profile's own backtest scores at 27 trades, 0% win.

---

## 2. The causes, ranked

Ranked by measured money where a realised number exists. The one cause that cannot be
priced in money sits where its importance belongs, first, and is labelled as such.

| # | cause | measured size | whose |
|---|---|---|---|
| 1 | **The machine was off most of the time.** Laptop lid closed → Modern Standby → hibernate. Dark 09-24 06:23Z–20:36Z (14.2 h, no log line, no gate row, no candle) and 09-25 12:16Z → 09-29 04:53Z (64h36m). Awake 28.6 of 43.25 nominal hours in the trading window (66%), about 29 of 132 hours since the start (22%). | **Not a realised loss.** Forgone P&L bounded by replay at +30 to +192 USDT (optimistic fills; the rule's own record is −1.29%/30d). The real cost is information: **6 distinct decisions** instead of the ~40 three awake days would give, and a 3.7-day hole in every series. | infrastructure (`outage-2026-09-25.md` §8 root cause 1) |
| 2 | **Hand flatten of Sleeve A**, "SELL EVERYTHING", 09-23 21:58:51Z (`audit_log` 36–37). BTC had fallen 1.54% between the entries and the flatten. | **−42.21 USDT (−0.21%)**, 70% of the cumulative loss | operator |
| 3 | **Sleeve B's double buy and phantom flatten**, 09-23 13:37Z → 13:52Z: bought 7,510 USDT of BTC/ETH against a 25%-of-NAV target, sold it all 15 minutes later on `targets:no_proposal_ever`. | **−27.56 USDT (−0.14%)**, 46% of the loss; **15.01 of it fees** on a 15-minute, 15,006-USDT round trip | code (a race in `strategies/`) |
| 4 | **The trend-loss exit produced every losing trade**: TRX ×2 (−10.44, held through the sleep; replay says the cross would have fired at 09:00Z for −1.25% had the machine been on) and BTC ×2 (−6.01). 0 wins in 4. | **−16.45 USDT (−0.08%)** against +26.13 from the rungs, trailing and ROI exits (6 of 6 won) | strategy design |
| 5 | **Fees ate half the gross.** 10.01 on 19.69 gross, at the dry run's 10 bps per side and zero slippage. Per-side measured cost 11.8–12.7 bps, i.e. about 0.24% per round trip, *below* the 0.30% planning floor only because paper fills land at the touch. | **−10.01 USDT (−0.05%) = 51% of gross.** At 0.30% the same ten trades net about +4.7 | cost structure of sub-1% rungs |
| 6 | **The gate's refusals** — 1,998 refused rows, but they are 5-second retries of 40 distinct refusals. | **≈ a wash**: about +30 forgone vs 33.8 saved (§3) | not a cause |
| 7 | **The model path produced nothing to trade.** 7 of 7 proposals abstained citing `assets={}` / `asof_candle_utc=null` (no computed indicators handed to the decision); 15 of 15 validations `uncertain` with an error; local model 0 scan calls since 09-23 (prompt 19–30k tokens vs an 8k window); cloud scan 0 of 11 calls succeeded before the 09-24 23:00Z fix. | **0 USDT lost, about 24 USD of model spend for zero output** (upper bound; two tables overlap). Sleeve B's real job was never tested. | code + data |
| 8 | **The market** | BTC +0.52% price-only over the window, basket +9.2%; to now BTC −1.13%, basket +5.7% | **not a cause** |

So, bluntly: the two biggest realised numbers are one-off plumbing and operator events on the
first evening, the biggest structural fault is a laptop that goes to sleep, and the strategy
that ran was a plumbing test that happened to have a good week. That is not a strategy
result in either direction; it is an absence of a test.

---

## 3. What the gate cost versus what it saved

4,054 gate rows in the window; 10 entries allowed per sleeve pair; every exit allowed (exits
are never blocked, and the SOL exit during an 80-second wake-up on 09-26 shows it). The
refusals, collapsed from 5-second retries into distinct decisions:

| reason | distinct refusals (both sleeves) | replayed if allowed (optimistic / conservative fill) | what it saved |
|---|---|---|---|
| `blackout:data_stale` (data too old to trust) | 14, but TRX was allowed 5 minutes later and 10 are the same five coins refused again under `beta_cap` | +55 / +19 gross of double counting; the only unique miss is ONDO on 09-24 06:05Z, about +5 per sleeve | — |
| `beta_cap` (book would move more than 1.3× BTC) | 14 | +54 / +26 | **PEPE 09-25 12:00Z: −11.31 avoided** |
| `staleness` (during the standby wake-ups) | 12 | +11 / +2 | **LTC 09-26 11:31Z: −22.50 avoided** |

Net of the double count, and honouring the satellite caps the replay ignores (a sleeve had
room for one more 5% position at 20:45Z on 09-24, not four): **about +30 USDT forgone
(optimistic) or +15 (conservative), against 33.8 saved. A wash.** The two data-stale
refusals that were later allowed cost a delay of about 4.5 USDT in worse prices (TRX 5
minutes, NEAR 8 minutes).

What never fired: the daily-loss stop (0 of 4,054 rows; the worst day was −0.42% against a 3%
trigger, and that day was the hand flatten), cooldown, gross cap, kill switch, fee budget.
There is nothing to replay for the daily stop this week; its measured verdict is already in
`crisis-policy.md` (flatten −5.23% vs halve +13.05% CAGR at the same trigger), and the
working copy has been changed to halve (§5).

Two things the gate did *not* do that matter more than its refusals. It did not see about
70 of the 91 entry signals the rule fired while the machine was awake, because the profile's
own `min_entry_spacing_min: 300` suppressed them before the gate (not journaled). And its
`beta_cap` check writes only true/false, not the beta it measured, so whether 1.30 is the
right line cannot be judged from the journal.

**Verdict: the gate is not where the money is, in either direction.** It did its job, cheaply.

---

## 4. Can a sub-1% profit rung ever clear a 0.30% round trip?

The owner asked: "isn't less than 1 percent profit per trade too less, won't this much go in
just execution costs". Arithmetic first, then what the ten trades and the studies say.

### 4.1 The arithmetic (the owner is right in direction)

| rung | share of the gain paid to costs at 20 bps (dry-run actual) | at the 30 bps floor | left at the floor |
|---|---|---|---|
| +0.6% | 33% | **50%** | +0.30% |
| +0.9% (the working copy's new first rung) | 22% | 33% | +0.60% |
| +1.2% | 17% | 25% | +0.90% |
| +1.5% (the working copy's new second rung) | 13% | 20% | +1.20% |
| +2.0% | 10% | 15% | +1.70% |

On the legs that actually fired: first-rung exits grossed 6.20 USDT and their share of
fees was 1.50 (24%; 36% at the floor); second-rung exits grossed 9.83, fees 1.41 (14%; 21%).
The fills landed above the rungs (average +0.83% and +1.41% against +0.6% and +1.2%), which
is a paper-trading kindness that live fills will not repeat.

### 4.2 What other rungs would have done on these same ten trades

Each trade's own recorded high and low was used, so no hindsight about the path.

| rung set | total net, 10 trades |
|---|---|
| as traded (fills above the rungs) | **+9.68** |
| exactly 0.6% / 1.2% | +6.07 |
| 1.0% / 2.0% | +8.86 |
| 1.5% / 3.0% | +12.60 |
| 2.0% / 4.0% (never reached → no ladder) | +9.63 |

**The whole range is under 3 USDT either way.** The rungs are a rounding error on this sample.
Stops at 4%, 6% and 8% give identical results on all 14 trades (the worst adverse excursion
was −1.41%). What did matter is the **trailing stop**: with the ladder off and a single
position held to the rule exits, the total is +5.08 with trailing on and **−49.85 with
trailing off** (INJ would have run into the 6% stop for −30.97; NEAR the trend-loss exit for
−15.11). The trailing stop, not the ladder, is what made the week positive.

### 4.3 The system, not the trade: this is a horizon problem

A single +0.6% trade clears a 0.30% cost. A *system* of many sub-1%, sub-day trades does not,
and that is measured, not argued:

- The profile's own 30-day backtest, costs on: **92 trades, −1.29%, fees 1.35% of balance**;
  the ROI exits won 49 of 49 (+2.59%) and the trend-loss exits lost 27 of 27 (−2.80%). The
  fees exceeded the loss. It "must not be left running" (its own header).
- `analogue-timing.md` §4.4: across 544 costed trades, **a one-day maximum hold is the only
  horizon in the study that is significantly negative** (mean gross −0.92%, t −4.56);
  break-even is about **ten days**; cost *level* barely matters (0 → 40 bps per side moves
  the good book's CAGR only 21.6% → 18.9%). The variable that matters is **cost per holding
  day**: 1.31 bps for the surviving configuration, **90.9 for this profile**.
- `analogue-timing.md` §4.5: the minimum-edge gate works only in its **holding-period form**
  — at k=5 it refuses 100% of 0.33-day intended holds and 96.7% of one-day holds while
  refusing 0.2% at 40 days. In the literal "target ≥ k × cost" form it refuses almost nothing
  (0 of 463 at k=3), because crypto volatility dwarfs 30 bps.

So the answer to "not fewer trades but better trades" is the measured one across every
study in this repository: **better trades are longer trades on fewer, older, calmer names,
sized to their volatility, entered on trend and left alone** — 18.7 trades a year at 22.9-day
holds in the surviving cell (§5 of `analogue-timing.md`), and the BTC/ETH trend ensemble
(`dip-strategy.md` §0.3: **43.1% CAGR / −45.6% worst drawdown / Sharpe 1.08 against holding
BTC's 38.6% / −83.2% / 0.58**, the first book in the project to beat holding BTC on all
three axes; §0.5: the owner's $20,000 over the last twelve months is $19,830 in that book
against $14,823 holding BTC). `growth-audit.md` §0 is the reason nothing else works: growth
in this market is almost entirely BTC-beta plus noise; only 3.4% of coins beat BTC in
2023-24; 77.5% of +100% run-ups were given back within 60 days — which is the argument *for*
taking profit on a plan, and the argument *against* trying to pick the coin.

One fairness note. The best trade of the week, ONDO, is one of the seven pairs
`growth-audit.md` §1.5 says would not pass the quality bar (with ENA, PENGU, XPL, ZEC, PUMP,
TRUMP). That is not a contradiction: the audit is about the median outcome over 49,833
coin-weeks; this was one seven-minute trade.

---

## 5. What was already fixed on 09-24/25, and what it changes

| fixed | when | what it changes | verified |
|---|---|---|---|
| `data_stale` flag now expires and ingest can clear it itself; `check_trading_blocked` and console supervision added to the 5-minute watchdog (`ops/healthcheck.py` docstring) | 09-24 | A stale-data block can no longer outlive the watchdog; silence is detected as a state | **Worked on 09-29**: ingest cleared the flag 8m41s after the resume, no human (`outage-2026-09-25.md` §6.3) |
| Cloud screening: `scan` max_turns 1 → 4, per-run cap $0.05 → $0.25, deadline 90 → 180 s; `classify` max_turns 1 → 2; the local context guard enforced (`config/models.yaml`, runtime working tree) | 09-24 23:00Z (09-25 03:00 Gulf), refined 09-25 | Cloud scan went from **0 of 11 calls succeeding to 18 of 20** | measured in `llm_calls` |
| Funding-phase fix that let ingest finish and clear the 09-24 flag | 09-24 21:40Z | ended the post-wake refusals that evening | `flags` row 13 |

What these fixes **do not** change, and the week shows it:

- **The host still sleeps.** Nothing in software substitutes (`outage-2026-09-25.md` §9).
  Hibernate-from-standby converted a closed lid into 63 hours off; 20 alert rows were written
  the morning it woke and **none was delivered anywhere** (`delivered=0`, no push channel).
- **The local model still screens nothing** (0 scan calls since 09-23): the prompt is 19,253–
  30,704 tokens against an 8,192 window, so every scan escalates to the cloud model at full
  price (`local-model-choice.md` §1). The fix is the prompt (20,024 → 3,896 tokens by
  rendering only the pairs in the batch, §8), then the model swap to `granite4.2:3b`.
- **The decision stage still has no indicators.** Every proposal in the window, including the
  four made on fresh candles, carried `assets={}` and abstained to cash; `growth-audit.md`
  §0 recorded the cause (8 daily candles per pair in the knowledge DB against the 201 the
  state computation needs). **The validator still errors on every call** (13 of 15 on an
  invented feature key that `local-model-choice.md` §5 traces to one detector,
  `dip_from_high`, declaring a `high_30d` it never computes).
- **The recovery this morning manufactured its own problems** (`outage-2026-09-25.md` §6.4):
  a rerun wrote **today's 16:00 Gulf proposal at 05:01Z from 3.7-day-old data** as a stale
  abstain, so the real 16:00 run will find the file and do nothing; `missed_run` is a flag
  nobody clears; "TRADING IS BLOCKED" keeps firing after trading was unblocked; the research
  log carries "research_run not permitted (level_below_required)", which may suppress
  today's runs regardless (`reports/daily/2026-09-28.md`, cause 1).
- **The bots tonight still run the old profile.** The containers have not restarted since
  09-24 03:32Z, so they carry the 0.6% / 1.2% rungs. The working copy's new rungs (§6, item 8)
  reach a bot only through regenerate-and-restart, which is a human action this review did
  not take.
- **Nothing is committed.** The runtime tree carries 233 modified files against `HEAD`
  (`72e755b`), the runtime's `profiles.active: fast-test` is uncommitted, and the committed
  tree has been unloadable since 09-24 01:39 Gulf (`autonomy.run` in the yaml, absent from
  the committed `ops/config.py`; `reports/daily/2026-09-28.md`, cause 3). The system that
  ran this week exists only on that disk.

---

## 6. The build list, in priority order

"Covered" means the parallel build workflow running today has already changed the working
copy for it (its own test file, `tests/test_foundation/test_risk_and_ladder.py`, names its five
config changes; `docs/design/trend-ensemble.md` names the ensemble build). "New" means nothing
in the working copy addresses it yet. Every item is tied to a number in this document.

| # | item | the number | status | who |
|---|---|---|---|---|
| 1 | **Keep the machine on, or move `~/earn-run` off the laptop.** Lid-close = do nothing on AC, hibernate-from-standby off; better, an always-on host. Add the off-host dead-man ping (`HEALTHCHECKS_URL`) and one push channel for `critical` rows. | Awake 29 of 132 hours (§2 #1); 64h36m dark; 20 alerts, 0 delivered (§5) | **New** — hardware/OS, human only | owner |
| 2 | **Show the cumulative pot on the console and never flatten a paper book by hand.** The ledger reset at 09-23 23:45Z hid −69.77; the flatten instinct is the policy `crisis-policy.md` measured worst. | −42.21 (§2 #2); console 20,009.68 vs cumulative 19,939.91 (§1.1) | **New** — console (tier 2) + a rule for the operator | human |
| 3 | **Fix Sleeve B's double-buy race and the phantom "no proposal ever" flatten.** A rebalance top-up was approved 68 ms after the proposal order, with `gross_exposure 0.0`, because the fill was not yet counted; 15 minutes later the mandate check saw no proposal and sold everything. | −27.56, of which 15.01 fees (§2 #3) | **New** — `strategies/` (tier 2) | human |
| 4 | **Hand the decision stage its indicators.** `compute_state.asset_state()` needs 201 daily candles; the DB holds 13. The ensemble build's `daily_closes` (feather ∪ DB) solves the same shortage for `trend.json`; the market-state file needs the same union. Until then every proposal is an abstain and Sleeve B cannot be tested. | 7 of 7 abstains on `assets={}`; 11.86 USD for nothing (§2 #7) | **Partly covered** — the data union exists in `runs/features/trend.py`; not applied to `latest.json` | human |
| 5 | **Make the validator produce verdicts.** Stop `dip_from_high` declaring `high_30d` it does not compute (13 of 15 errors); fix the two schema errors; run more than one validation per scan so screened signals stop expiring (38 of 53 did). | 15 of 15 `uncertain`, 6.84 USD; 98 of 100 candidates never reached a decision (§2 #7) | **Check** — the build workflow modified `runs/signals/validator.py` and `screener.py`; confirm it closes the `unknown feature_key` error | build workflow / human |
| 6 | **Local tier: slim the scan prompt, then swap the model** (`granite4.2:3b`, `"think": false`, `CHARS_PER_TOKEN` 3.5 → 2.0). Swapping the model alone changes nothing while the prompt does not fit. | 0 local scan calls since 09-23; 19–30k tokens vs 8k; 20,024 → 3,896 tokens with the slim prompt (§5) | **Covered** — `prompts/stages/scan.v2.md`, `runs/llm/chain.py`, `runs/signals/screener.py` in the working copy; the `models.yaml` alias swap is not yet in it | build workflow |
| 7 | **Wire the trend ensemble in and run it.** Built this morning (`runs/features/trend.py`, `strategies/trend_state.py`, gate in `strategies/earn_base.py`), reproduced to the decimal (43.07 / −45.62 / 1.08), **not yet wired**: until `write_state` runs after ingest, core entries on both sleeves are shut with `trend_state_missing`. Today's signal: BTC and ETH both weight 1.0, 15 of 15 members on. | The only book that beats holding BTC on return, drawdown and Sharpe (§4.3); $19,830 vs $14,823 on the owner's $20,000 over 12 months | **Covered (built), wiring New** — `trend-ensemble.md` §4 lists the two steps | human |
| 8 | **Rungs vs the cost floor, and the minimum-edge gate in the form that works.** The working copy moved the rungs to 0.9% / 1.5% and added `risk.min_edge` (smallest booked target ≥ 3 × 0.30%). That fixes the per-trade arithmetic (50% → 33% to costs) and is the right hygiene. It is **not** the measured gate: `analogue-timing.md` §4.5 shows the target-vs-cost form refuses almost nothing and the **minimum-holding-period form** (k=5) is what stops sub-day trading. Add the dual form. | Rungs move the 10-trade result by < 3 USDT (§4.2); one-day holds −0.92% gross, break-even ≈ 10 days (§4.3) | **Covered (rungs, literal min_edge)**; **dual form New** | build workflow / human |
| 9 | **Daily-loss stop: halve, not flatten.** Done in the working copy (`daily_loss_response: halve`, trigger kept at 3%). It never fired this week (worst day −0.42%), so it changes nothing about this result; it is insurance against the next bad day. | −5.23% vs +13.05% CAGR at the same trigger (§1.2) | **Covered** | build workflow |
| 10 | **Stop running the plumbing profile as if it were the strategy, and decide what the next test is.** The profile's header says it must not be left running; its fee budget is exhausted in ~22 days; spacing + 4 trades/day + 66% uptime made 5 entries per sleeve the ceiling. The next test is the shipped 4-hour sleeves with the ensemble gate, judged on §7's checks for 30 days. | −1.29%/30d, fees 1.35% (§1.2); 6 decisions in a week (§2 #1) | **New** — a decision for the owner, then regenerate + restart | owner |
| 11 | **Rerun and alert plumbing after a sleep** (`outage-2026-09-25.md` §9): daily jobs rerun on a later day must name their artifact by the slot, not by "now" (today's 16:00 decision was pre-empted at 05:01Z); `research_run` must refuse to write a proposal for a slot that has not happened; `missed_run` must expire and clear; `acting()` must end the "blocked" verdict when data is fresh; dedupe must not require delivery; reruns must wait for DNS. | one lost decision slot today; 207 false criticals on 09-24/25; 4× "TRADING IS BLOCKED" after unblocking (§5) | **New** — `ops/` (tier 2) | human |
| 12 | **Measurement hygiene so the next review is faster and truer**: exclude `status='fallback'` partial-exit legs from the TCA page (they show −70 to −157 bps of "slippage" that is a stale mid, not execution); journal the beta `beta_cap` measured and the spacing suppressions in `SleeveFast`; point the holdings watcher at the run database it is meant to watch (it saw `holdings: 0` in all 111 cycles while TRX, BTC and SOL were open); confirm with one test that the trailing stop's recorded `stop_loss_pct −0.0059` is the from-current bookkeeping of `mechanics.stoploss_from_open` (the arithmetic reproduces it exactly: max +1.84%, current +1.64% → −0.59%) and not a tighter stop. | §3, §4.2 | **New** — tier 2 | human |
| 13 | **Reconcile the runtime with git.** 233 modified files against `HEAD`; committed tree unloadable since 09-24; `profiles.active: fast-test` uncommitted. Nothing built today should be deployed on top of an unrecorded state. | §5 | **New** — human only (this workflow never commits) | owner |
| 14 | **News labels.** The `hack` rule fired a full research run on the venture firm "Hack VC" (0.85 USD); the `lawsuit` substring rule labels "Security", "second" and "pursuit" (31 rule labels, 51% precision). No mislabel moved money. | ≈ 1.2 USD; 0 orders | **Partly covered** — `settlement` keyword fix in the working copy; the substring rule itself is not | build workflow / human |

Not on the list, deliberately: changing the rungs further, widening the gate's beta cap,
loosening spacing, or "letting winners run" on the fast profile. §4.2 shows the rungs are
noise on this sample, §3 shows the gate was a wash, and `dip-strategy.md` §0.1 and
`analogue-timing.md` §0 found no intraday or coin-selection edge to tune. Tuning a plumbing
profile is polishing the wrong object.

---

## 7. What eight more hours of paper trading can and cannot tell us

**Cannot: anything about return or edge.** `dip-strategy.md` §10: "30 days cannot tell you
about return. Thirty days can only tell you whether the mechanism is behaving. Anything
presented as a 30-day verdict on edge is a lie, including a good one." Its §6.3 puts the
time to separate the recommended book from holding BTC at 86 years. Eight hours of the fast
profile will add one to four trades per sleeve (the profile's own note: 2.88 fills per
10-hour window on average, 11% chance of none), taking the sample from 10 to perhaps 16 —
all of them sub-day, all of them the profile whose 30-day record is −1.29%. Nothing in
tonight's P&L, up or down, should change a single decision above.

**Can: whether the mechanism behaves**, and specifically whether the three things that went
wrong this week — the host, the exits, the model path — behave tonight. The five checks to
read tomorrow morning, in order; if the first fails, the other four do not count.

| # | check | pass line | where it comes from |
|---|---|---|---|
| 1 | **Was it on?** Bot heartbeats in every hour from 05:00Z 09-29 to the morning; zero `staleness` or `blackout:data_stale` gate refusals; `data_stale` inactive; `nav_points` ticking every 15 minutes for both sleeves. | 100% of hours | §2 #1: 66% uptime was the first cause. A missing hour means the laptop slept again and item 1 of §6 was not done |
| 2 | **Did entries happen, and were they sized as designed?** 1–4 entries per sleeve; every one ≈ 500 USDT (5% of NAV); never more than 4 per Gulf day; every allowed gate row with all 26 checks true; `daily_lock` never fires (with 5% positions it cannot, so if it does something is broken). | as stated | `fast-test.yaml` note; §3 |
| 3 | **Did the exits behave?** Every first-rung leg ≥ +0.6% gross and second-rung ≥ +1.2%; every trailing exit within about 0.8% of the trade's own high in price terms; zero 6%-stop exits; trend-loss exits counted separately with their P&L (the profile predicts they lose). | as stated | §4.2; §6 item 12 (the `stop_loss_pct` question is answered by this check) |
| 4 | **What did costs take?** Fees ÷ gross on the new trades below about 35% is consistent with the rungs; above 50% again is the profile confirming its −1.29% backtest. Per-side cost in `tca_rolling` steady near 12 bps (paper); a jump is a data fault, not execution. | < 35% | §2 #5; §4.1 |
| 5 | **Did the model path produce anything?** Did the 16:00 Gulf research run actually run, or `return 0` on the abstain pre-written at 05:01Z? Does the newest proposal still cite `assets={}` / `asof_candle_utc=null`? Are today's validations still `uncertain` with `unknown feature_key`? Does `knowledge/state/trend.json` exist yet? | a real 16:00 run; a populated `assets`; at least one validation with a verdict | §5; §6 items 4, 5, 7 |

If all five pass, the correct sentence is "the plumbing works", not "the strategy works"
(`dip-strategy.md` §10.1). If checks 2–4 pass and 5 fails, Sleeve B is still untested and
the model spend is still buying nothing. If check 1 fails, this document's first build item
is the only one that matters.

**What this implies about the next test.** The honest reading of the week is that the
strategy barely traded, the losses were operator and infrastructure, and the profit was a
plumbing profile having a good week. So the next test is not "more of this". It is: a host
that stays on; the shipped 4-hour sleeves with the trend-ensemble gate wired and running (the
book that would be fully invested in BTC and ETH today); Sleeve B fed real indicators so its
proposals can be something other than an abstain; the cumulative pot on the console; and a
30-day scorecard made of `dip-strategy.md` §10.1's five mechanism checks — exposure tracks
the signal, fee drag ≤ 0.15% of NAV per 30 days, turnover 0.4–1.0× NAV, drawdown inside
−12%, losing less than BTC on BTC's ten worst days — with §10.2's stop triggers armed. Run
that, and in a month we will know whether the machine does what it was designed to do. We
will still not know whether it earns; the documents say that takes years, and say so in
numbers. What we will have stopped doing is losing money to a closed lid and a typed
"SELL EVERYTHING".

---

## Appendix A — where the brief and the journal disagree

Reported as measured, so the next narrative starts from the record.

| the brief said | the journal shows |
|---|---|
| A 14-hour `data_stale` wedge refused 655 entries on 09-24, 03:00–20:00Z | The host slept 06:23Z–20:36Z (Windows event log; zero rows of any kind in the gap). Refusals were 1,506 five-second retries of 7 distinct signals per sleeve, at 01:00Z, 06:05–06:23Z and 20:36–20:45Z. `ops/healthcheck.py`'s docstring still tells the wedge story; `outage-2026-09-25.md` §7 corrects it |
| Bot A lost −42.21 when the orchestrator restarted its container and force-exited on 09-24 | `audit_log` 36–37: `human:console` `autonomy.flatten a`, confirmation "SELL EVERYTHING", 2026-09-23T21:58:51Z. Container restarts that day were at 13:58Z, 14:35Z, 15:40Z and 23:37Z; none coincides |
| Nothing traded after 09-25 12:15Z | Sleeve B's SOL exit filled at 2026-09-26T00:07:46Z during an 80-second standby wake-up (+3.54) |
| BTC fell about 2% in the first stretch | Over the trading window BTC rose +0.52%; the closest measured fall is −1.54% between Sleeve A's entries (13:37Z) and the flatten (21:58Z) on 09-23 |
| The console shows a 20,000 pot that is worth less | The console ledger shows 20,009.68; it reset to 20,000 at 23:45Z on 09-23 and does not include the evening's −69.77 |

## Appendix B — provenance and limits

- Sources: `ft_userdata/{a,b}/runs/test-{a,b}-000.sqlite` (the fast-profile run databases the
  bots write to), `ft_userdata/{a,b}/tradesv3.sqlite` (the 4-hour strategies until 23:37Z on
  09-23), `journal/journal.db` (`gate_decisions` 4,054 rows, `fills` 43 = the bots' 43 filled
  orders, `nav_points`, `runs`, `proposals`, `audit_log`, `llm_calls`, `signals`,
  `signal_validations`), `knowledge/earn.db` (`candles` 1h for all 31 pairs, `news_items`,
  `funding`, `ops_incidents`), both `freqtrade.log` files (heartbeat minutes as the uptime
  measure), the Windows event log via `outage-2026-09-25.md`. All copied to `/tmp` and opened
  `mode=ro`. Design documents cited: `crisis-policy.md`, `dip-strategy.md`,
  `analogue-timing.md`, `growth-audit.md`, `local-model-choice.md`, `ml-forecast.md`,
  `trend-ensemble.md`, `outage-2026-09-25.md`, and the evidence block of
  `config/profiles/fast-test.yaml`.
- Sample: 14 trades, 10 under the profile, and those are 6 distinct decisions duplicated
  across two sleeves. No statistical claim is made or possible; `edge-audit` would refuse a
  row count of 6.
- Every fill is a paper fill at the touch: no queue, no partial fills, no adverse selection.
  Realised cost (11.8–12.7 bps per side) understates live cost; 0.30% per round trip is the
  planning number.
- Replays are 1-hour-candle approximations of a bot that decides every 5 seconds, seeded
  with each trade's recorded high and low, pessimistic inside a candle. Calibrated on the ten
  real trades the optimistic variant lands within 0.21 USDT of reality in total but not per
  trade; every replay figure is a range, not a point.
- The bots' REST endpoints could not be read (the documented ports are a proxy that returns
  `not_found`; the real ports take a password injected from `.env`, which this review may not
  read). The pot is computed from the bot databases and `nav_points`, which agree.
- The trailing-stop discrepancy in §6 item 12 is explained by reading `strategies/mechanics.py`,
  not by running it.
- Nothing was written under `~/earn-run`; no bot, cron job, unit, console or order was
  touched; nothing was committed.
