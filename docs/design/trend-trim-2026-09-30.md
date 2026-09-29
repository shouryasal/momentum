# The trim — giving SleeveA a way to make a position smaller

Status: built 2026-09-30 against `docs/design/exit-and-horizon-2026-09-29.md` §5, which is the
whole mandate. Nothing here is switched on by this document: both sleeves are in TEST, the
change ships in `strategies/`, and no bot, cron, unit or console was touched, no commit was
made, no venue was called and nothing was written into `~/earn-run`.

**This is not an edge claim and must not be read as one.** The trim scores 1.12 in
risk-adjusted terms against a deflated hurdle of about 2.32 at roughly 8,134 cumulative
selection trials. It clears nothing. The case for it is exactly two sentences, both of which
need no hurdle:

1. **The code should do what its own design document says it does.** `trend-ensemble.md` was
   justified by a book whose exposure equals the ensemble weight, re-sized as members switch
   off. The shipped sleeve read that weight at two BUY-side call sites only, and
   `earn_base.py` said so in as many words — *"It never touches an exit"*. Neither
   `SleeveA._desired_stake` (returns `0.0`) nor `_sleeve_adjust` (returned `None`) could
   produce a negative stake, so a rising weight bought more and a falling weight merely
   bought **less**.
2. **It should not breach its own exposure limits on a third of all days.** `weight_cap`,
   `gross_cap` and `usdt_floor` all have the shape `(position + stake) / nav <= cap`: they
   refuse an *order*, so a position that grows past its cap because the price moved raises no
   order and the risk gate never sees it. Measured over 3,326 days the never-trimming sleeve
   sat above its gross ceiling on 136 days, above its BTC cap on 422 (12.7%), above its ETH
   cap on 265 and under its USDT floor on 136, peaking at **0.921 of NAV against its own 0.80
   ceiling**. With the trim: **zero, on all four, on every one of the 3,326 days.**

---

## 1. What changed

Three files, no new config key, no new parameter, no new machinery beyond one pure
classification function.

| file | what | tier |
|---|---|---|
| `strategies/SleeveA.py` | `_trim_plan` — the whole trim, 40 lines; `_sleeve_adjust` asks it first and falls through to the calendar DCA unchanged | 2 |
| `strategies/earn_base.py` | `_SELLABLE_TREND_REASONS` + `_trend_weight_for_trim` (the fail-closed read, 6 lines); `STRATEGY_VERSION` → `earn-4`; the `_trend_weight` docstring corrected, because it says something that was true and is not | 2 |
| `strategies/mechanics.py` | `TRIM_DRIFT`, `TRIM_BREACH`, `trim_reason()` — the exit-reason classification, pure and stdlib | 2 |
| `tests/strategies/test_trend_trim.py` | 44 tests; 15 of them fail with the trim disabled | 2 |

The rule, in one line: **when a core position exceeds `ensemble_weight × target_weight × NAV`
by more than the shipped 5% rebalance band, sell the difference.** The weight is the one
`custom_stake_amount` and `_gated_add` already use; the band is `execution.rebalance_band`.
Nothing else moves. In particular:

* `populate_exit_trend` is **untouched** — `regime_1d == 0` still takes the book flat, and
  `_trim_plan` returns `None` whenever the target is zero, so the 200-day flip still owns
  every full close in a bear (constraint 1).
* the exit MA lookback is untouched (constraint 2);
* no trailing stop and no time stop were added (constraint 3);
* no config key was added, edited or read for the first time (constraint 5);
* `config/**`, `runs/**`, `ops/**`, `console/**`, `strategies/SleeveB.py` and
  `strategies/SleeveFast.py` were not touched.

### 1.1 Where it sits in the candle

`_trim_plan` is offered in the **`rebalance`** slot of `mechanics.ACTION_PRIORITY`, which is
the last one. A pending flatten, a take-profit rung and an add all beat it. Two things sit
above even that, structurally rather than by priority: `_mechanics_plan` returns before any
candidate is built when `gate.flatten_pending(now)` is set, and freqtrade's `process()` calls
`exit_positions` — where the stop, ROI, the exit signal and `custom_exit` live — **before**
`process_open_trade_positions`, so a full exit closes the trade before any adjustment
callback is asked about it. Both are pinned by tests.

Priority is not the same question as classification. The trim can be the last candidate
offered in a candle and still be an exit the fee budget may not silence; §4 is about that.

### 1.2 `STRATEGY_VERSION` → `earn-4`

Every journal row carries it, and `trend-ensemble.md` set the convention by moving it to
`earn-3` when the ensemble became an entry gate. `earn-4` is that ensemble allowed to make a
position smaller as well as larger. Without the bump, the Gate page cannot separate rows
produced by the trimming sleeve from rows produced by the one that could only buy — which is
precisely the comparison the first weeks of paper trading will want. Nothing pins the string
outside `earn_base.py` and one line of `trend-ensemble.md` §3, so the bump is free.

---

## 2. The replay: both anchor rows reproduced

An independent harness, written from `exit-and-horizon-2026-09-29.md` §2/§5 rather than
adapted from `evals/research/exit-horizon/asym_books.py`, on
`~/earn-panels/panel_1d.parquet`, 3,326 days, 2017-08-17 → 2026-09-24, 15 bps per side, one
full day of signal lag, cash 0%. Where the repo already owns a number the harness **imports**
it instead of re-deriving it — `runs.features.trend.ensemble_weight`,
`sleeve_common.regime_series` / `realized_vol_annual` / `core_satellite_targets`,
`mechanics.within_band`, `mechanics.trim_reason`, and every limit straight out of
`config/riskgate.json`. That is the point of a replication: the indicators and the
classification under test are the shipped ones.

Stated tolerance before running: **±0.75pp on CAGR, ±0.05 on Sharpe, ±1.50pp on MaxDD.**

| book | CAGR measured / anchor | Sharpe measured / anchor | MaxDD measured / anchor |
|---|---|---|---|
| **B — as deployed** | **21.41%** / 21.41% | **0.834** / 0.83 | **−45.77%** / −45.77% |
| **D — deployed + trim** | **14.67%** / 14.67% | **1.120** / 1.12 | **−20.64%** / −20.64% |
| BTC buy-and-hold | 38.59% | 0.827 | −83.19% |

Both anchors land inside the tolerance with room to spare — the largest deviation anywhere is
0.004 on B's Sharpe, which is rounding. Supporting rows also match the finding to the
decimal: mean gross 0.261 → 0.146, peak gross 0.921 → 0.642, fees 0.19%/yr → 0.29%/yr,
turnover 1.29× → 1.95× NAV/yr, 96 trims in 9.11 years.

### 2.1 Breach days, before and after

Counted on the book's own exposure each day, against the limits in `config/riskgate.json` —
the thing no order-time check can see.

| limit | shipped | B as deployed | anchor | **D with the trim** |
|---|---|---|---|---|
| gross exposure | 0.80 | **136 days** | 136 | **0** |
| BTC weight cap | 0.40 | **422 days** | 422 | **0** |
| ETH weight cap | 0.30 | **265 days** | 265 | **0** |
| USDT floor | 0.20 | **136 days** | 136 | **0** |
| peak gross reached | 0.80 | **0.921** | 0.921 | **0.642** |

All four counts reproduce exactly, and all four go to zero. This is the finding's central
deterministic claim and it carries no p-value.

### 2.2 Reproducing it

The harness is **not in the repo**: `evals/**` is outside this change's ownership, so it sits
at `~/earn-trim-harness/trim_replay.py` on this host (and in the session scratchpad). Whoever
owns `evals/` should move it to `evals/research/exit-horizon/trim_replay.py`; it is
self-contained, imports nothing that imports it, and takes about twenty seconds.

```bash
cd ~/earn-wk/tr1 && EARN_REPO=$PWD nice -n 10 \
  ~/earn-dev/.venv/bin/python ~/earn-trim-harness/trim_replay.py \
  --panel ~/earn-panels/panel_1d.parquet
```

It exits non-zero if either anchor falls outside the stated tolerance.

---

## 3. The 2022 check — and an honest discrepancy

Constraint 1 exists because a pure scale-out lost 20.6% in 2022 where the deployed MA200 flip
lost 6.7%: the scale-out held residual exposure all the way down while the 200-day average
went flat in January and stayed flat. I kept the flip. Here is the year, on my own harness,
per calendar year (total return / worst drawdown inside the year):

| year | B as deployed | **D with the trim** | S — trim with the MA200 flip REMOVED |
|---|---|---|---|
| 2019 | +16.09% / −26.55% | +18.65% / −15.33% | +17.98% / −15.81% |
| 2020 | +94.30% / −18.18% | +51.78% / −14.28% | +52.34% / −13.78% |
| **2021** | +56.40% / **−45.29%** | +22.99% / **−8.62%** | +26.24% / −9.11% |
| **2022** | **−1.53%** / −2.01% | **−0.85%** / −1.10% | −1.05% / −1.40% |
| 2023 | +31.89% / −10.93% | +18.07% / −10.77% | +17.86% / −10.77% |
| 2024 | +29.42% / −21.14% | +27.30% / −12.32% | +25.56% / −12.38% |
| 2025 | −4.56% / −14.91% | **+2.06%** / −10.69% | +1.32% / −11.03% |

**2022: the trim is better, not worse.** −0.85% against the deployed −1.53%, with a shallower
in-year drawdown (−1.10% against −2.01%). The constraint holds.

**The discrepancy I have to report.** The −6.65% the brief quotes for 2022 is a *signal-level*
number — the construction with exposure latched at entry, no vol targeting and no 10% stop,
which carries about 0.45 of NAV on average. My B carries 0.261 and D carries 0.146. At that
exposure level the whole 2022 argument shrinks: **removing the MA200 flip entirely (book S)
costs 0.20pp in 2022 and 0.16pp of Sharpe over the full window, not 14 points.** So my harness
does **not** reproduce the 14-point 2022 gap the constraint was written from, because that gap
lives at a different exposure level than the shipped sleeve's.

That does not change the decision, for two reasons. The flip is *weakly dominant* in my own
construction — D beats S on CAGR (14.67 vs 14.63), Sharpe (1.120 vs 1.113), drawdown (−20.64
vs −20.80) and 2022 (−0.85 vs −1.05) — so keeping it is free here and expensive to remove at
any other exposure level. And the constraint is an instruction, not a hypothesis I was asked
to re-test: I kept the flip, and book S is reported only so nobody has to take that on trust.

---

## 4. The exit classification — the decision, and why

Constraint 4, in the brief's own words: *a "rebalance" exit is throttleable by the churn and
monthly fee-budget checks (crisis-policy.md G6/G7) — a trim that the fee budget can silence is
not a risk control.*

### 4.1 The decision

**Split it, on the state of the book rather than on the size of the trim.**
`mechanics.trim_reason` asks three questions of the book *before* the trim — the same three
sizing checks the gate applies to an incoming order, asked of the position actually held:

| condition | reason | what the gate does |
|---|---|---|
| position / NAV > its weight cap | `risk_stop_exposure` | `is_risk_exit` → **waved through**; crosses the spread; does not consume the day's order count |
| gross / NAV > the gross ceiling | `risk_stop_exposure` | as above |
| free USDT / NAV < the USDT floor | `risk_stop_exposure` | as above |
| none of the above | `rebalance_trim` | discretionary: orders-per-day, turnover and the monthly fee budget all apply, and may refuse it |

The reason travels three ways from one string: into `check_discretionary_exit`, onto the
`AdjustPlan`'s **tag**, and into the `partial_exit` journal row the Gate page reads.

The tag is the part worth spelling out, because it was free and is easy to miss.
`FreqtradeBot.check_and_call_adjust_trade_position` passes a negative-stake adjustment's
`order_tag` straight into `execute_trade_exit(..., exit_tag=order_tag)`, and there
`exit_reason = exit_tag or exit_check.exit_reason`. So the tag *becomes the partial exit's own
exit reason*, which means:

* `custom_exit_price` receives it as `exit_tag` and, for `risk_stop_exposure`, crosses the
  spread (`bid × (1 − cross_ticks_buffer)`) instead of resting on the ask;
* `order_filled` → `record_order_fill(risk_exit=True)` keeps a breach trim off the
  discretionary `orders_per_day` counter, so a de-risk can never be starved by the limit it
  just tripped;
* the trade's own record says *why*, instead of the bare `partial_exit` freqtrade would
  otherwise invent.

Neither reason starts with any of `_COOLDOWN_EXITS`, so **neither branch arms the 24-hour
re-entry cooldown** — which is right: a trim is not a stop-out, and a book that could not buy
back when the ensemble recovered would pay for the trim twice.

`risk_stop_exposure` is recognised through the `risk_stop` **prefix**
(`mechanics.RISK_EXIT_PREFIXES`), which is exactly what `crisis-policy.md` G7 item 8 asks for.
It is deliberately **not** added to `RISK_EXIT_REASONS`: `riskgate.flatten_pending` returns
only the two exact names `risk_stop_daily` / `risk_stop_monthly`, and keeping the new name out
of the set means it can never be mistaken for a sleeve-level flatten.

### 4.2 Why a split rather than one classification

A single `rebalance` classification fails the brief's own test: a de-risk a fee budget can
silence is not a de-risk. A single `risk_stop_*` classification is worse in a quieter way — it
would make a routine 6%-over-band drift immune to every churn control the repo has, cross the
spread to pay for the privilege, and hide 96 sells over nine years from the `orders_per_day`
counter that exists to notice a sleeve that has started churning. The split gives the fee
budget authority over housekeeping and none at all over a breach.

### 4.3 The evidence that the split actually works

Two numbers from the replay, and they are the ones that decide it.

**First: in 9.11 years the breach branch never had to fire.** All 96 trims in book D are
`rebalance_trim`. The drift trim fires as soon as the excess passes 5% of NAV, which is well
inside every cap, so the book never reaches a breach to be rescued from. Prevention is the
whole mechanism, and the breach branch is a backstop.

**Second: the backstop is sufficient on its own.** Book Dr is the pathological case — *every*
drift trim refused, as a permanently exhausted fee budget or order counter would refuse it,
1,675 refusals over the window, leaving only the trims a limit breach authorises:

| book | trims | Sharpe | MaxDD | peak gross | gross / BTC / ETH / USDT breach days |
|---|---|---|---|---|---|
| B as deployed | 0 | 0.834 | −45.77% | 0.921 | 136 / 422 / 265 / 136 |
| **Dr — breach branch only** | **8** | **1.015** | **−26.66%** | **0.681** | **0 / 0 / 0 / 0** |
| D — both branches | 96 | 1.120 | −20.64% | 0.642 | 0 / 0 / 0 / 0 |

Eight un-refusable trims in nine years are enough to keep the book inside all four of its own
limits on all 3,326 days. That is the property the classification exists to guarantee, and it
is the answer to the question the split raises: *what happens when the throttle bites?* The
book loses some of the drawdown benefit (−26.66% instead of −20.64%) and none of the limit
compliance.

### 4.4 Closing honest limit 3 of the finding

`exit-and-horizon-2026-09-29.md` limit 3 says Book D's trim *"was measured without the
exit-side churn caps that would actually apply to it (16 orders/day, 50% turnover/day, 1% fee
budget/month) … Unlikely to bind at 1.95× turnover; not modelled."* Modelled now, per Gulf day
and Gulf month, with every notional normalised by the NAV at the time:

| check | shipped limit | B as deployed | **D with the trim** |
|---|---|---|---|
| worst orders in one Gulf day | 16 | 2 | **2** |
| worst Gulf-month fees | 1.00% of NAV | 0.136% | **0.091%** |
| worst Gulf-day turnover | 50% of NAV | **55.7% — 1 day over** | **46.5% — 0 days over** |
| drift trims landing where any churn check could refuse them | — | — | **0 of 96** |

Two things fall out. The caps do not bind: eleven times the fee headroom and eight times the
order headroom. And the deployed book already has one Gulf day over the 50% turnover cap — its
own opening entry — which the trim's smaller positions *remove*. So the churn caps are not a
reason against the trim in either direction, and the one day they would have bitten is a day
the trim deletes.

---

## 5. What I copied from SleeveB, and where its version is wrong

`SleeveB._sleeve_adjust` (lines 324-389) already trims toward a standing target, and the brief
asked me to copy that shape and to say where it has a bug rather than inherit one silently.
The shape I took: a negative-stake `AdjustPlan`, `check_discretionary_exit`,
`_clamp_to_exchange`, `_cost_basis_exit`, a deferred `partial_exit` journal row. Five places I
deliberately did not follow it.

1. **SleeveB trims against `ps.committed(pair)`, which includes resting entry orders.** Its
   gap is `target × NAV − committed`, and `committed = position + pending`. A resting BUY can
   therefore push the gap negative while the *held* position is still under target, and
   SleeveB will sell coins it holds to offset an order that has not filled. It needs the
   resting order to exceed the target by more than the 5% band, which is reachable exactly
   when a new proposal lowers the target while an add is resting — a proposal-driven sleeve's
   normal life. `committed` is the right view on the **buy** side (it is the fix for the
   2026-09-23 double buy) and the wrong one on the sell side, where the only thing you can
   sell is what you hold. SleeveA's trim measures `trade.amount × current_rate`, which a
   resting order cannot inflate.
2. **SleeveB computes its sell fraction across two price marks.** Its trim size comes from
   `ps.committed()` (valued at `_last_price`) and its denominator from
   `trade.amount × current_rate`. The two marks differ by a tick in live trading, so the
   fraction handed to `_cost_basis_exit` is very slightly wrong in an unpredictable direction.
   SleeveA keeps the numerator and denominator of the fraction on the same mark
   (`current_rate`).
3. **SleeveB can silently do nothing at all when the remainder is dust.**
   `check_and_call_adjust_trade_position` computes what would be left and bails out entirely
   with *"Remaining amount would be smaller than the minimum"* — it refuses the **whole**
   partial exit, not just the crumb. SleeveB has no escalation for that;
   `_unwind_exhausted` handles a different case (a position walked all the way down) and is
   keyed on the band and `min_position_pct_nav`, not on the exchange's minimum exit stake.
   For SleeveA this would have killed the single most important trim there is — the one at
   ensemble weight zero is by definition a 100% trim — so `_trim_plan` reuses
   `ladder_step`'s own `full_exit_below_min` rule and `_cost_basis_exit(full_exit=True)`,
   which returns the cost basis on the nose so `remaining` is exactly 0.0 and freqtrade
   proceeds. Pinned by `test_selling_the_lot_asks_for_the_exact_cost_basis`.
4. **SleeveB's trim carries no tag, so the console calls it a profit rung.** With no tag
   freqtrade falls back to `exit_check.exit_reason`, which for a partial exit is the literal
   string `partial_exit` — and `console/web/src/pages/portfolio/why.ts` maps that to
   *"profit rung — A take-profit rung sold part of the position."* With the shipped ladder
   empty that label can never be true: every SleeveB rebalance trim is currently mislabelled
   on the Portfolio page. SleeveA's trim names itself. See §7 for the console follow-up.
5. **SleeveB takes a `rebalance.min_interval_hours` cadence; SleeveA's trim does not.** The
   band is already self-limiting — once the trim has run, the excess is zero, and price has to
   move the position more than 5% of NAV past its scaled target again before anything fires —
   and the measurement had no cadence, so adding one would have been a deviation. More to the
   point, a cadence is one more throttle on a risk control, which is the thing constraint 4 is
   about.

One sharp edge I could **not** avoid, because avoiding it needs new machinery in
`earn_base.py` that this change does not justify: the `partial_exit` journal row is committed
when the plan wins the priority contest, which is *before* freqtrade decides whether to place
the order. `handle_similar_open_order`, a zero rounded amount or the dust remainder above can
all still decline it, and the Gate page would show a trim that did not happen. This is a
property of the `AdjustPlan` contract shared by the take-profit ladder and both sleeves, not
something the trim introduces — but item 3 removes its commonest cause for this path.

---

## 6. Acceptance — each item and the test that fails without it

`tests/strategies/test_trend_trim.py`, 44 tests. Run with the trim disabled (one early
`return None` in `_trim_plan`), **15 of them fail**; the rest are guard tests that assert the
trim does *not* fire, and they must pass either way.

| what is pinned | test |
|---|---|
| a position 10 points of NAV over its ensemble-scaled target is trimmed by the excess | `TestTheTrimSellsTheExcess::test_a_position_ten_points_of_nav_over_its_scaled_target_is_trimmed_by_the_excess` |
| …and `adjust_trade_position` returns the same negative stake and tag | `::test_the_dispatcher_returns_the_same_negative_stake` |
| one trim reaches the scaled target; the next candle is quiet | `::test_one_trim_reaches_the_scaled_target_and_the_next_candle_is_quiet` |
| a position inside the band is not touched; 6% over is | `::test_a_position_inside_the_band_is_not_touched` |
| the buy side still tops up a position under its target | `::test_a_position_under_its_scaled_target_still_buys` |
| the trim can never exceed the position | `::test_the_trim_can_never_exceed_the_position` |
| selling the lot asks for the exact cost basis (or freqtrade refuses all of it) | `::test_selling_the_lot_asks_for_the_exact_cost_basis` |
| the market value is converted onto the trade's cost basis | `::test_the_trim_is_converted_onto_the_trades_cost_basis_not_its_market_value` |
| **fail closed**: missing / unparseable / stale / warming-up / absent asset ⇒ NO TRIM | `TestFailsClosedToNoTrim::test_no_trend_file_means_no_trim`, `::test_an_unparseable_file_means_no_trim`, `::test_a_stale_bar_means_no_trim`, `::test_a_warming_up_asset_means_no_trim`, `::test_an_asset_the_file_does_not_carry_means_no_trim` |
| the sellable set is exactly `ok` and `weight_zero` | `::test_the_sellable_reasons_are_exactly_a_real_weight_and_a_real_zero` |
| a satellite is never trimmed | `::test_a_satellite_is_never_trimmed` |
| a fail-closed refusal is journalled, so a silenced trim is visible | `::test_the_refusal_is_journalled_so_a_silenced_trim_is_visible` |
| the MA200 flip still produces the exit signal, and owns target zero | `TestTheBinaryFlipKeepsTheClose::test_the_exit_signal_is_still_the_ma200_flip`, `::test_target_zero_is_the_flips_business_not_the_trims` |
| routine drift is a discretionary `rebalance_trim` the fee budget may refuse | `TestTheExitClassification::test_routine_drift_is_a_discretionary_rebalance`, `::test_a_drift_trim_is_refused_when_the_fee_budget_is_gone` |
| a cap breach is a risk exit the fee budget may **not** refuse | `::test_a_cap_breach_is_a_risk_exit_the_fee_budget_may_not_silence` |
| the breach branch crosses the spread; the drift branch rests on the ask | `::test_the_breach_branch_crosses_the_spread_and_the_drift_branch_does_not` |
| a breach trim does not consume the day's discretionary order count | `::test_a_breach_trim_does_not_consume_the_days_discretionary_order_count` |
| neither branch arms the re-entry cooldown | `::test_neither_branch_arms_the_re_entry_cooldown` |
| the journal row says which branch fired (and a refusal names the check first) | `::test_the_journal_row_says_which_branch_fired`, `::test_a_refused_trim_journals_the_check_first_and_the_branch_behind_it` |
| the trim cannot run while a flatten is pending | `TestNothingIsOutRanked::test_a_pending_flatten_beats_the_trim` |
| the ladder takes priority via `ACTION_PRIORITY`; the trim is last | `::test_a_take_profit_rung_beats_the_trim`, `::test_the_trim_is_the_lowest_priority_candidate_in_the_candle` |
| freqtrade runs every full exit (stop, ROI, exit signal) before any adjustment | `::test_freqtrade_runs_every_full_exit_before_it_asks_for_an_adjustment` |
| an invalid NAV suppresses the trim | `::test_an_invalid_nav_suppresses_the_trim` |
| the risk gate still authorises every trim, and its checks reach the journal | `TestTheGateStillAuthorisesIt::test_every_trim_goes_through_check_discretionary_exit`, `::test_the_gate_decisions_checks_ride_into_the_journal_row` |
| the trim is floored onto the exchange lot grid; too small is no trim | `::test_the_trim_is_floored_onto_the_exchange_lot_grid`, `::test_a_trim_the_exchange_would_reject_is_no_trim` |
| `trim_reason` itself: drift, each of the three breaches, exactly-at-a-limit, the prefix, no new number | `TestTrimReason` (9 tests) |

Suites run in `~/earn-wk/tr1`: `tests/strategies` + `tests/test_riskgate` + `tests/contract`
= **600 passed**; `ruff check .` clean.

---

## 7. Honest limits

1. **Not an edge claim, and not close to one.** 1.12 against a deflated hurdle of ~2.32 at
   ~8,134 trials. The finding's own bootstrap puts the trim's Sharpe gap at +0.287 with
   p = 0.080 on a single nine-year path of two assets on one venue. The deterministic results
   — 136/422/265/136 breach days → 0, peak exposure 0.921 → 0.642 — carry no p-value and are
   what this change stands on.
2. **This is not a Freqtrade backtest.** It is a sleeve-level reconstruction using the shipped
   indicator and classification code. `exit-and-horizon-2026-09-29.md` limit 2 already said
   replaying SleeveA through Freqtrade over the same window is the confirming test and that
   nobody had run it; still nobody has. It should be run before this ships to LIVE. A 4h
   sleeve can also act up to 20 hours earlier than a daily reconstruction, which the replay
   does not model.
3. **The 2022 constraint does not reproduce at this exposure level**, and I have reported the
   number rather than the expectation (§3). Removing the flip costs 0.20pp in 2022 here, not
   14 points. I kept the flip anyway; book S exists so the reader does not have to take that
   on trust.
4. **The breach branch is proven by a counterfactual, not by the base case.** All 96 trims in
   the replay are drift trims. The eight breach trims in book Dr are the only direct evidence
   that branch works on real data, and they come from a book in which every drift trim was
   artificially refused. Everything else about the breach branch is proven by unit test.
5. **The full-exit escalation never fires in the replay** (0 of 96 trims). It exists for a
   freqtrade refusal path that the panel cannot produce, because the panel has no exchange
   filters and a NAV normalised to 1.0 cannot express a 25-USDT minimum notional. It is
   proven by unit test only.
6. **Satellites are out of scope.** `not_core` is excluded from the sellable set, so a
   satellite that drifts above its 5% cap still generates no order and the gate still cannot
   see it. That is the same defect this document fixes, one tier down, and it is untouched
   because the measurement was BTC/ETH.
7. **The replay's classification input is very slightly kinder than the live code's.** The
   harness updates its running gross within the candle as each leg trims, so the second leg
   sees the post-trim gross; the live code reads one `PortfolioState` snapshot taken before
   any of them. The live code therefore classifies *more* trims as breaches than the replay
   does, never fewer.
8. **The journal row for a trim is written before freqtrade agrees to place the order** — §5's
   unavoidable edge. A refused order leaves a `partial_exit` row on the Gate page. Shared with
   the ladder and SleeveB; not introduced here.
9. **Under KILL the trim still trims.** `check_exit`'s own rule is that exits reduce risk and
   are always allowed, including under KILL, and `_mechanics_plan` consults only
   `flatten_pending`. This matches the ladder and SleeveB. An operator who wants nothing at
   all to move uses the console's stop, not the kill file.
10. **One number in `docs/design/trend-ensemble.md` §3.2 is now wrong** and I did not edit it,
    because that file is outside this change's ownership: it says the ensemble is *"Never an
    exit"* and that *"a test asserts `custom_exit` is `None` at weight 0 with an open
    position"*. The test is still true and still passes — the trim goes through
    `adjust_trade_position`, not `custom_exit` — but the sentence above it needs amending, and
    so does §6 limit 3, which says *"this build cannot"* reduce exposure as members switch
    off. It can now.

---

## 8. Follow-ups for other owners

| what | who | why |
|---|---|---|
| Two labels in `console/web/src/pages/portfolio/why.ts`: `rebalance_trim` → *"trend trim"*, `risk_stop_exposure` → *"over its limit — trimmed"*. And fix `partial_exit`, which currently claims *"profit rung"* for every SleeveB trim | console | §5 item 4. Unknown reasons fall back to the raw text, so the trim reads as *"rebalance trim"* until then — honest but plain |
| Point `runs.features.trend.check_exposure_tracks_signal` at the strategy, not only at the plumbing | `runs/` | It would have caught this years of backtest ago. The finding measured it failing on 1,103 of 3,069 days against the deployed rules; with the trim it should be 44 (1.4%) |
| Move `~/earn-trim-harness/trim_replay.py` into `evals/research/exit-horizon/` | `evals/` | §2.2. It is the only way to re-run this measurement |
| Replay SleeveA through Freqtrade over 2017-08 → 2026-09 with the trim on | `evals/` | Limit 2. The confirming test, still unrun |
| Amend `docs/design/trend-ensemble.md` §3.2, §6 limit 3 and §3's `earn-3` row | docs | Limit 10, and §1.2. Its limit 5 ("`write_state` is not wired") is also out of date — `runs/ingest.py:336` wires it |
| The peg / halt gap (G4), which is worth more attention than any of this | `runs/` | `exit-and-horizon-2026-09-29.md` §3 |

---

## 9. Deploying it

`ops/docker-compose.yml` bind-mounts `../strategies` into both containers read-only, and
freqtrade loads the strategy class at process start — so the change reaches the bots only on a
**restart of both paper bots**. Nothing else moves: `config/**` is untouched, so
`config.bless.json` does not need re-blessing (`BLESSED_FILES` is the five config files only),
`python -m ops.gen_freqtrade_config --check` and `ops.gen_ops_files --check` both report no
drift, and no cron or unit changes.

The trim is live the moment the bots restart, because `runs.features.trend.write_state` **is**
wired now (`runs/ingest.py:336`) — `trend-ensemble.md` limit 5 is out of date.
`~/earn-run/knowledge/state/trend.json` was last written 2026-09-29T20:30Z on the bar that
closed 2026-09-29T00:00Z, with **BTC 1.000 and ETH 1.000, 15 of 15 members on**.

What that means for the first hour:

* At weight 1.0 the scaled target *is* the plain target, so a trim fires only if a position
  has drifted more than 5% of NAV above its own vol-targeted target weight. **Expect no trim
  on the first candle.** One `trend_gate:ok` gate-decision row per core pair is the normal
  sign of life.
* A `rebalance_trim` in the first candle means the paper book was already above its target —
  interesting, and exactly what this change is for.
* A **`risk_stop_exposure` in the first candle means the book was already in breach of a cap**
  when the bots restarted. The finding says that is the case on 12.7% of days for BTC, so it
  is not unlikely. It is the correct response, and it is the one trim no churn check can
  refuse.
* Watch for `trend_gate:trend_state_stale` or `:trend_state_missing` on the
  `adjust_trade_position` callback. That is the new fail-closed path saying the daily writer
  has stopped, and it means the trim is switched off until it comes back. It is a plumbing
  alarm, not a market signal.
* Journal rows now stamp `earn-4`. Anything still reading `earn-3` came from before the
  restart.

---

## Sources

Mandate and every anchor number: `docs/design/exit-and-horizon-2026-09-29.md` §2, §5, §6, §7.
Cited, not re-derived: `docs/design/trend-ensemble.md` (§3.2 and its limit 3, which declared
this asymmetry without pricing it), `docs/design/crisis-policy.md` G6/G7 and §5.3 item 8,
`docs/design/dip-strategy.md` §0.3, `docs/design/wide-universe.md` §2.4. Code read and quoted:
`strategies/SleeveA.py`, `SleeveB.py`, `earn_base.py`, `mechanics.py`, `riskgate.py`,
`trend_state.py`, `sleeve_common.py`, `runs/features/trend.py`, `config/riskgate.json`,
`console/web/src/pages/portfolio/why.ts`, and freqtrade 2026.8's
`freqtradebot.check_and_call_adjust_trade_position` / `execute_trade_exit` / `process`.
Measured on `~/earn-panels/panel_1d.parquet` with `~/earn-trim-harness/trim_replay.py`.
