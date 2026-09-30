# Track 4 — Cadence: how often do we look, and is that right?

**Run date** 2026-09-30 · **Workspace** im4 · **Data** `~/earn-run/data/binance/*.feather` (read-only,
31 whitelist pairs, 2017-08-17 → 2026-09-23) and the repo working copy. The live
`knowledge/earn.db` and `journal/journal.db` were **not opened, copied or read** at any point.

**Verdict — the shipped cadence is already right, and the owner's instinct that we are too slow
is refuted by measurement.** Reaction lag up to **48 hours** costs nothing on the signal we
actually trade. Looking *faster* makes the book worse. The only cadence problem in the system is
not on the trading side at all: it is that a 15-minute cron job holds the bot's *permission* to
enter, with only 30 minutes of headroom, and that is what caused both of this month's multi-hour
entry blackouts.

---

## HALF ONE — the facts: what runs, how often, and what it can actually change

Read from `ops/crontab`, `config/earn.yaml` (`ops.schedules`, `signals.scanner`, `watch`,
`research.slots`, `trading`, `risk`), `config/freqtrade-{a,b}.json`, `strategies/earn_base.py`,
`strategies/SleeveA.py`, `strategies/SleeveB.py`, `strategies/riskgate.py`, `runs/ingest.py`,
`ops/lib/freshness.py`.

### 1.1 The three clocks (this is the distinction that matters)

There are **three different clocks** in Earn, and conflating them is what makes the system feel
slow when it is not.

| Clock | Period | Source | What moves on it |
|---|---|---|---|
| **Loop clock** | **~5 seconds** | `freqtrade-{a,b}.json: internals.process_throttle_secs: 5` (heartbeat 60 s) | `bot_loop_start` → `gate.loop_tick` (KILL, risk flatten, monthly unlock), `custom_exit`, `custom_stoploss`, `confirm_trade_entry/exit`, `adjust_trade_position`, and SleeveB reading the newest approved proposal file |
| **Decision clock** | **4 hours** | `config/earn.yaml: trading.timeframe: "4h"` + `earn_base.py: process_only_new_candles = True` | `populate_entry_trend` / `populate_exit_trend`. This is the **only** moment the entry or trend-exit *signal value* can change: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC |
| **Signal clock** | **1 day** | `@informative("1d")` in both sleeves; `regime_1d` = close vs 200-day SMA with 2% hysteresis; `rvol_1d` for sizing | SleeveA's entire entry rule, its entire exit rule, and its vol sizing. Nothing in SleeveA's entry or exit reads the 4h frame at all — the 4h frame supplies only ATR (disabled) and the moment of action |

**SleeveA is a daily strategy running on a 4-hour clock.** That is a fact from the code, not an
opinion: `populate_entry_trend` is `regime_1d > 0`, `populate_exit_trend` is `regime_1d == 0`.

A risk flatten, a stop-loss, a KILL file and a freshly written proposal are all honoured on the
**5-second** clock. Entries and the trend exit are honoured on the **4-hour** clock. The owner's
"how fast does it react" has two different answers depending on which one you mean.

### 1.2 The full job table

Cron expressions are Gulf time (`CRON_TZ=Asia/Dubai`, UTC+4, no DST), exactly as
`config/earn.yaml: ops.schedules` declares them.

| Job | Cadence | Times/day | What it can actually change | Latency from a market move to a possible order |
|---|---|---|---|---|
| **freqtrade loop** (a and b) | every **5 s** | 17,280 | Gate flatten, KILL, monthly unlock, stop-loss, ROI/trailing exit, order timeouts, SleeveB's adoption of a new proposal | **~5 s** for any of those |
| **4h candle close** | every **4 h** | 6 | `enter_long` / `exit_long` signal values; the sleeve rebalance decision | 0–4 h of pure quantisation on top of the signal's own age |
| **1d informative close** | every **24 h** | 1 | `regime_1d`, `rvol_1d` — i.e. all of SleeveA | see §1.3 |
| `ingest` (`runs.ingest`) | `*/15 * * * *` | 96 | Writes candles (incl. the **in-progress** bar, `is_closed=0`), book snapshots, news, funding into `knowledge/earn.db`; stamps `knowledge/state/freshness.json`; **clears** a non-human `data_stale` flag | None directly — but it holds the gate's *permission* to enter (§2.3) |
| `scanner` (`runs.signals scan`) | `*/5 * * * *` | 288 | 10 detectors → candidate signals → LLM screen (`max_candidates_per_cycle: 5`, `dedupe_minutes: 240`) → may fire the planner | **Cannot place an order.** Fastest path: scan (5 min) → validate (`min_confidence 0.65`, ≤6/day, 120 min per-asset cooldown, expires after 90 min) → planner (`min_verdict: valid`, **≤3/day, 4 h cooldown**) → research run → proposal file → SleeveB adopts within 5 s → order at the **next 4 h close** |
| `watch` (holdings watcher) | `*/7 * * * *` | ~206 | **Only** a `watch_events` row and an escalation request. `runs/watch/guard.py` enforces that it can never place, size or cancel an order, write a proposal or change config | **Zero. It can never cause an order.** |
| `nav_tick` | `*/15 * * * *` | 96 | NAV telemetry | None |
| `reconcile` | `*/15 * * * *` | 96 | Can set the `reconcile` blackout flag; `reconcile.block_on_mismatch: true` → **blocks entries** | ~15 min to *block*; never to enter |
| `healthcheck` | `*/5 * * * *` | 288 | Can set `data_stale` (blocks entries); can write `ops/killdir/KILL` on a mode mismatch | KILL honoured within ~5 s |
| `tca_job` | `5 * * * *` | 24 | Cost telemetry; `tier1_freeze` flag after 14 days above 1.5× assumed cost | Days |
| `nav_job` | `10 0 * * *` | 1 | NAV row | None |
| **`research_run`** | `30 8` and `0 16` Gulf (04:30 / 12:00 UTC) | **2** | **The LLM decision.** Writes `proposals/YYYY-MM-DD-HHMM.json` → SleeveB's target weights | The only *scheduled* path from Claude to a position. Adopted within 5 s, executed at the next 4 h close |
| `daily_review` | `30 21 * * *` | 1 | Report, lessons | None |
| `discovery_light` | `20 2 * * *` | 1 | Research candidates | None |
| `discovery_deep` | `0 4 * * 6` | weekly | Research candidates | None |
| `review_run` | `0 20 * * 0` | weekly | Weekly review, `changes/*.json` proposals | Needs human approval |
| `backtest_data` | `0 18 * * 0` | weekly | Refreshes `data/binance/*.feather` | None |
| `backup` | `0 3 * * *` | 1 | Backup | None |
| `maintenance` | `0 2 * * 1` | weekly | DB maintenance | None |

### 1.3 The measured latency chain — from a price move to an order

Worst case, SleeveA, trend exit or entry:

| Step | Delay | Why |
|---|---|---|
| Price crosses the 200-day MA (say 00:05 UTC on day D) | — | The MA is a daily-close construct; an intraday cross is not a signal |
| The 1d candle for day D closes | up to **23 h 55 m** | `regime_1d` is computed on the 1d **close** |
| Freqtrade's informative merge makes it visible | **0 h** | `merge_informative_pair` sets `date_merge = informative_date + 1d`, which is exactly when the close became knowable — **no artificial extra day** |
| The first 4h candle carrying it closes (04:00 UTC on D+1) | **4 h** | 4h quantisation. The 4h bar dated 00:00 D+1 closes at 04:00 D+1 |
| Order placed | **~5 s** | The loop clock |
| Limit order fills | up to **20 min** | `execution.entry_unfilled_timeout_min: 20`, bid-side limit, `market_entries_allowed: false` |

**Worst case ≈ 28 h 15 m. Average ≈ 14 h.** For a gate flatten, a stop-loss or a KILL the same
chain is **~5 seconds**, because those do not wait for a candle.

Additional brakes, all from `config/earn.yaml`: `rebalance.min_interval_hours: 4`,
`risk.cooldown_candles: 2` (= 8 h at 4h), `risk.max_trades_per_day: 4`,
`risk.max_orders_per_day: 16`, `risk.max_turnover_pct_per_day: 0.50`,
`signals.planner.cooldown_hours: 4`.

### 1.4 How often are we "analysing"? The funnel

| Stage | Looks per day | Cap |
|---|---|---|
| Detectors fire (`runs.signals scan`) | **288 cycles/day** | `max_candidates_per_cycle: 5`, `dedupe_minutes: 240` |
| LLM screen (`task: scan`, `min_score 0.60`, gray zone 0.45–0.65) | up to ~1,440 candidate-screens/day in principle, dedupe-bounded in practice | — |
| LLM validate (`task: validate`) | **≤ 6/day** | `validator.max_per_day: 6`, `cooldown_min_per_asset: 120` |
| LLM decide (research run) | **≤ 5/day** (2 scheduled + ≤3 planner-fired) | `planner.max_per_day: 3`, `cooldown_hours: 4` |
| Moments a position can change | **6/day** | the 4h candle closes |
| Trades actually permitted | **≤ 4/day/sleeve** | `risk.max_trades_per_day: 4` |

So 288 looks per day funnel into at most 5 model decisions and at most 4 trades. The detectors
already cover, per `signals.scanner.detectors`: `volume_spike` (1h, z-score 3.0 over a 72-bar
lookback — this is the owner's "recent buy volume"), `move` (1h 2.5% / 4h 5.0% / 24h 8.0%),
`breakout` (1d, 20-day, confirm-on-close), `dip_from_high` (1d, 12% over 30 days), `rsi_extreme`
(4h, 14, 25/75), `funding` (|8h| ≥ 0.0010), `near_stop` (fast path), `regime_flip`, `news_event`
(corroborated only; hack/depeg/delist on the fast path). `ma_cross` is **disabled**.

---

## HALF TWO — does the cadence cost or save money?

### Pre-registration

Sealed before any number was computed, to
`<scratchpad>/prereg-track4.json`. (The `hypothesis-lab` script writes to
`knowledge/state/hypotheses/**`, which is tier 2 and inside the read-only live root for this run,
so the seal lives in the scratchpad instead. Stated as a limitation, not worked around.)

| # | Statement | Falsifier (written first) |
|---|---|---|
| **H1** | Evaluating the shipped entry rule more often than the shipped 4h cadence raises net return after 0.30% round trip | If net return at 1h evaluation is not ≥ **1.0 pp/yr above** 1d evaluation on the same sample, and turnover rises, looking more often is refuted |
| **H2** | Acting on the next closed candle instead of the bar the signal appeared on costs measurable net return at every timeframe | If the cost of the one-bar delay is **under 0.25 pp/yr at 4h**, the lag complaint is refuted |
| **H3** | A 15-minute ingest cadence against a 30-minute staleness gate leaves entries blocked on a non-trivial fraction of decision moments | If **fewer than 1%** of 4h decision moments fall inside a data gap wider than the gate's allowance, staleness is not a practical drag |

**Method.** 31 pairs — the `config/freqtrade-a.json` whitelist, i.e. the names the system may
actually authorise. Signal reconstructed from the code: 200-day SMA of 1d closes, 2% hysteresis
(`sleeve_a.trend`), long when above and flat when below, with the informative merge modelled
exactly (the 1d bar dated D becomes usable at D+1 00:00). Cost **15 bps per side / 30 bps round
trip**, always on, charged on every change of position. Sharpe is the repo arithmetic form —
mean/std of **daily** equity returns × √365 — for every book, whatever grid it was computed on, so
the numbers are comparable. Regime split on BTC's own 200-day MA.

### Two measurement errors I made and caught — read this before trusting any table

The discipline earned its keep twice in this study, and both traps are ones a careless version of
this report would have shipped as findings.

1. **A return-grid artefact that looked like a 4.8 pp lag cost.** My first pass compared books
   computed on *different* return grids (1d close-to-close vs 4h vs 1h) and produced a beautifully
   monotone "every hour of lag costs ~1 pp of CAGR" curve: 1d 7.44%, 1h 5.52%, 4h 2.60% median.
   Re-running everything on **one common 1h grid** with turnover and fees held identical made the
   curve vanish. The 1.9 pp gap between two books with *identical timing* on different grids is
   the honest calibration of this study's measurement noise: **median-across-31-coins CAGR is
   only meaningful to about ±2 pp.** Every median difference below that in the tables below is
   noise.
2. **A look-ahead leak that made looking *less* often look spectacular.** Quantising the
   evaluation grid with `resample(f'{Q}h').last()` labels bins on the **left**, so the value
   assigned to time *T* was observed at *T+Q−1h* — up to 335 hours of look-ahead at Q=336. It
   produced portfolio Sharpe **2.17** and CAGR **111%**, monotone in Q, improving 31/31 pairs.
   Fixing the label offset collapsed it to Sharpe 0.73–0.83. A number that improves monotonically
   with how long you wait is the signature of leaked future information, not of patience.

Both discarded runs are still counted as trials below.

### H1 — decision frequency vs net return

Leak-free. All books on the identical 1h return grid; only *how often the position may change*
varies. "Slow signal" = the shipped daily regime. "Fast signal" = the same 200-day MA level but
compared against the **live 1h price**, i.e. the fastest possible intrabar trigger.
`fee` is cumulative cost as a fraction of starting capital over the whole ~9 years.

**Per-coin medians across the 31 (n = 31 unless noted)**

| Signal | Look every | med CAGR | mean CAGR | med Sharpe | med MaxDD | med round trips | med fee drag | med position changes |
|---|---|---|---|---|---|---|---|---|
| slow (daily) | **1 h** | 5.64% | 4.90% | 0.407 | −80.3% | 20.5 | 6.15% | 41 |
| slow (daily) | **4 h — SHIPPED** | 3.19% | 5.66% | 0.376 | −80.8% | 17.5 | 5.25% | 35 |
| slow (daily) | 12 h | −0.08% | 5.99% | 0.333 | −80.7% | 17.5 | 5.25% | 35 |
| slow (daily) | 24 h | 1.09% | 6.92% | 0.346 | −81.8% | 17.5 | 5.25% | 35 |
| slow (daily) | 72 h | 2.08% | 9.39% | 0.379 | −76.9% | 14.5 | 4.35% | 29 |
| slow (daily) | 168 h | 3.28% | 9.66% | 0.323 | −76.8% | 11.5 | 3.45% | 23 |
| slow (daily) | 336 h | −1.50% | 5.94% | 0.263 | −79.9% | 7.5 | 2.25% | 15 |
| fast (intrabar) | **1 h** | 0.24% | 2.53% | 0.346 | −81.8% | **33.5** | **10.05%** | 67 |
| fast (intrabar) | 4 h | 1.18% | 3.96% | 0.376 | −81.9% | 31.5 | 9.45% | 63 |
| fast (intrabar) | 12 h | 5.53% | 5.41% | 0.437 | −82.0% | 28.5 | 8.55% | 57 |
| fast (intrabar) | 24 h | 4.83% | 4.80% | 0.394 | −80.1% | 23.5 | 7.05% | 47 |
| fast (intrabar) | 72 h | 3.11% | 7.41% | 0.394 | −79.7% | 16.5 | 4.95% | 33 |
| fast (intrabar) | 168 h | 3.86% | 9.89% | 0.391 | −78.3% | 11.25 | 3.38% | 22.5 (n=30) |
| fast (intrabar) | 336 h | 2.10% | 7.37% | 0.329 | −76.1% | 8.25 | 2.48% | 16.5 (n=30) |
| **buy & hold, same 31** | — | 6.43% | −0.09% | **0.543** | −94.0% | 0.5 | 0.15% | 1 |

**Equal-weight portfolio of the same 31** (the headline — medians are too noisy here)

| Signal | Look every | CAGR | Sharpe | MaxDD |
|---|---|---|---|---|
| slow (daily) | 1 h | 22.38% | 0.720 | −55.2% |
| slow (daily) | **4 h — SHIPPED** | 22.81% | **0.730** | −54.0% |
| slow (daily) | 12 h | 23.84% | 0.750 | −52.8% |
| slow (daily) | 24 h | 25.15% | 0.777 | −53.3% |
| slow (daily) | 72 h | 27.03% | 0.813 | −50.2% |
| slow (daily) | 168 h | 28.25% | **0.834** | −53.0% |
| slow (daily) | 336 h | 23.40% | 0.732 | −59.6% |
| fast (intrabar) | 1 h | 21.62% | 0.705 | −55.3% |
| fast (intrabar) | 4 h | 22.63% | 0.728 | −55.0% |
| fast (intrabar) | 12 h | 23.30% | 0.742 | −54.7% |
| fast (intrabar) | 24 h | 22.60% | 0.726 | −54.3% |
| fast (intrabar) | 72 h | 24.72% | 0.771 | −53.0% |
| fast (intrabar) | 168 h | 28.35% | 0.842 | −51.2% |
| fast (intrabar) | 336 h | 26.23% | 0.799 | −54.5% |
| **buy & hold, same 31** | — | **41.46%** | **0.841** | −88.5% |

**Paired per-coin difference vs the shipped 4h cadence** (slow signal)

| Look every | mean ΔCAGR | median ΔCAGR | pairs improved |
|---|---|---|---|
| 1 h | **−0.75 pp** | −0.31 pp | **15 / 31** |
| 4 h | 0 | 0 | — |
| 12 h | +0.33 pp | +0.41 pp | 18 / 31 |
| 24 h | +1.26 pp | +1.94 pp | 21 / 31 |
| 72 h | **+3.73 pp** | +3.65 pp | 23 / 31 |
| 168 h | **+4.00 pp** | +3.40 pp | 23 / 31 |
| 336 h | +0.29 pp | +0.24 pp | 16 / 31 |

**Paired: fast intrabar signal minus slow daily signal, at the same look cadence**

| Look every | mean ΔCAGR | median ΔCAGR | pairs improved |
|---|---|---|---|
| 1 h | **−2.37 pp** | −1.80 pp | **11 / 31** |
| 4 h | −1.69 pp | −2.70 pp | 11 / 31 |
| 12 h | −0.58 pp | −1.44 pp | 13 / 31 |
| 24 h | −2.12 pp | −2.62 pp | 11 / 31 |
| 72 h | −1.98 pp | −1.70 pp | 13 / 31 |
| 168 h | −0.36 pp | 0.00 pp | 14 / 30 |
| 336 h | +1.18 pp | +1.06 pp | 17 / 30 |

**H1 verdict: REFUTED.** The falsifier required 1h evaluation to beat slower evaluation by
≥1.0 pp/yr. It does the opposite: 1h is **−0.75 pp** against the shipped 4h and improves only
**15 of 31** pairs — a coin flip. Looking *less* often is mildly better, peaking around 3–7 days
at +3.7 to +4.0 pp CAGR and portfolio Sharpe 0.81–0.83, and the mechanism is transparent:
round trips fall from 17.5 to 11.5–14.5 and cumulative fee drag from 5.25% to 3.45–4.35% of
capital. **A faster signal is strictly worse at every practical cadence** — the intrabar trigger
doubles round trips (17.5 → 33.5) and fee drag (5.25% → 10.05%) and loses 1.7–2.4 pp/yr,
improving only 11 of 31 pairs.

**But nothing here is shippable.** Every single cadence loses to holding the same 31 coins
(CAGR 41.46%, Sharpe 0.841). The best cadence variant reaches Sharpe **0.834** against the
deflated hurdle of **~2.32**. This is a cost-reduction observation, not an edge.

### H2 — reaction lag, priced

The cleanest experiment in this report: **identical signal, identical turnover, identical fees —
only the hour of execution moves.** The position is taken *L* hours after the daily close that
generated it, on the 1h grid. L=4 is the shipped bot. Turnover is 17.5 round trips and fee drag
5.25% at **every** L, by construction, so any difference is pure timing.

**Paired per-coin difference against acting immediately (L = 0)**

| Lag | mean ΔCAGR | median ΔCAGR | pairs improved | mean ΔSharpe | median ΔSharpe |
|---|---|---|---|---|---|
| 1 h | (baseline-adjacent) | +0.04 pp | — | +0.004 | — |
| 2 h | **+0.01 pp** | +0.04 pp | 16 / 31 | −0.000 | +0.001 |
| 3 h | +0.18 pp | +0.45 pp | 18 / 31 | +0.003 | +0.005 |
| **4 h — SHIPPED** | **+0.26 pp** | +0.02 pp | 16 / 31 | **+0.007** | +0.003 |
| 6 h | +1.26 pp | +1.11 pp | 17 / 31 | +0.026 | +0.003 |
| 8 h | +0.89 pp | +0.56 pp | 17 / 31 | +0.010 | +0.006 |
| 12 h | +0.51 pp | +0.92 pp | 16 / 31 | +0.016 | +0.011 |
| 16 h | +0.76 pp | +0.58 pp | 18 / 31 | +0.011 | +0.013 |
| 24 h | +1.51 pp | +1.53 pp | 20 / 31 | +0.034 | +0.031 |
| **48 h** | **+3.95 pp** | +2.82 pp | 19 / 31 | **+0.112** | +0.031 |

**Equal-weight portfolio by lag**

| Lag (h) | 0 | 1 | 2 | 3 | **4** | 6 | 8 | 12 | 16 | 24 | 48 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CAGR | 23.09% | 22.84% | 22.92% | 23.44% | **23.13%** | 23.56% | 23.79% | 23.70% | 22.96% | 25.33% | 27.17% |
| Sharpe | 0.736 | 0.730 | 0.732 | 0.743 | **0.736** | 0.745 | 0.750 | 0.748 | 0.732 | 0.781 | **0.816** |
| MaxDD | −53.8% | −55.2% | −54.0% | −54.3% | −54.0% | −52.4% | −51.7% | −52.8% | −51.9% | −53.3% | −50.1% |

**Regime split** (median across the 31 of the daily mean return; median pair has **1,147 bull
days** and **944 bear days** on BTC's own 200-day MA)

| Lag (h) | 0 | 1 | 2 | 4 | 8 | 12 | 24 | 48 |
|---|---|---|---|---|---|---|---|---|
| Bull daily mean | +0.185% | +0.192% | +0.193% | +0.189% | +0.196% | +0.199% | +0.194% | +0.176% |
| Bear daily mean | −0.084% | −0.084% | −0.084% | −0.089% | −0.087% | −0.086% | −0.082% | −0.073% |

Flat in both regimes. The lag does not hurt in the bull leg and does not help in the bear leg.

**H2 verdict: REFUTED.** The falsifier asked whether the one-bar delay at 4h costs less than
0.25 pp/yr. It costs **nothing at all**: the shipped 4h lag is **+0.26 pp mean / +0.007 Sharpe
better** than acting instantly, and the win rate of 16/31 says the effect is indistinguishable
from zero in either direction. Delay remains free out to **48 hours** (+3.95 pp mean, and the
portfolio's best Sharpe of the whole sweep). The reason is structural and obvious once stated:
**a 200-day moving average does not move materially in two days.** You cannot be late to a signal
that is itself two hundred days slow.

**Both directions, as required.** What the lag avoided: nothing measurable. What the lag gave up:
nothing measurable. Neither the upside nor the downside of the shipped rule is sensitive to when
in the day it acts. This is a null result, and it is the useful kind — it says the entire
"we react too slowly" thread is not where the money is.

### H3 — staleness in practice

Two independent routes, neither of which touched the live journal.

**Route 1 — the candle record's own gaps.** Census of all 1h feather files, 31 pairs.

| Metric | Value |
|---|---|
| 1h bars present | 1,439,154 |
| 1h bars missing from the continuous grid | **946 (0.0657%)** |
| Gaps wider than 60 min | 296 |
| Gaps wider than 90 min | 296 |
| Gaps wider than 24 h | 3 |
| Worst single gap | **34 h** (BTC, ETH, LTC — one shared exchange outage) |
| Median pair | 14 missing bars, 7 gaps > 90 min over its life |

Blocked-decision census — the fraction of 4h decision moments at which the newest available 1h
bar is older than the gate's allowance:

| Blocking feed | Allowance | 4h decision moments | Blocked | Share | Median pair | Worst pair |
|---|---|---|---|---|---|---|
| `candles_1h` | 90 min (60 min `open_time` convention + `risk.staleness_minutes: 30`) | 360,043 | **175** | **0.0486%** | 0.0159% | 0.130% (BTC/ETH) |
| `book_snapshots` | 30 min (**no grace**) | 360,043 | **207** | **0.0575%** | 0.0239% | 0.146% (LTC) |

**Route 2 — the headroom arithmetic, from the code.** `ops/lib/freshness.py` blocks entries on
the **worst** blocking source, where `BLOCKING_SOURCES` is the order book plus the
`candles_<tf>` family, each candle feed allowed to be one timeframe old. `runs/ingest.py:
refresh_candles` inserts the **in-progress** bar (`is_closed = int(k[6] < now_ms)`) and
`freshness_sources()` takes a plain `MAX(open_time)` over all rows regardless of `is_closed`.

| Blocking feed | Written by | Cadence | Limit | Headroom before entries stop |
|---|---|---|---|---|
| `candles_1h` | `ingest` | 15 min | age − 60 min ≤ 30 min | With ingest healthy the effective age is **0**. Blocked once `now − floor(last successful ingest, 1h) > 90 min`, i.e. after **30–90 min** of ingest downtime |
| `book_snapshots` | `ingest` | 15 min | age ≤ 30 min, **no grace** | **The binding constraint.** One missed run puts it exactly at the limit; the **second** missed run blocks every entry. Headroom = **30 minutes = 2 cron slots** |

**H3 verdict: REFUTED as stated, and the real answer is worse than the hypothesis.** The
falsifier required under 1% of decision moments to fall inside a blocking gap; the measured figure
is **0.049%** for candles and **0.058%** for books. The *market data* is essentially never stale —
Binance's own outages cost us roughly one blocked 4h decision in two thousand.

But the two multi-hour entry blackouts this month were not data gaps at all:

- **2026-09-24, ~14 hours.** `runs/ingest.py`'s own docstring records it: healthcheck latched
  `data_stale` at 06:00, healthcheck then stopped running, and since only healthcheck could clear
  the flag the gate refused every entry for 14 hours. The fix shipped — ingest may now clear it —
  but the shape of the failure is the point.
- **2026-09-30, 6 h 42 m.** `ingest_runs` wrote no rows from 07:00:03Z to 13:41:56Z while
  healthcheck logged `database is locked` on three separate checks, coinciding with heavy
  read-copies of the live databases. (I am taking this from the relayed context; I did **not**
  open the journal to confirm it, per the snapshot-only rule.)

**So the staleness risk is ~100% pipeline and ~0.05% market.** And there is a structural oddity
worth naming plainly: **freqtrade prices its own orders off live ccxt data fetched every loop, but
its *permission* to place them depends on a separate 15-minute cron job writing a JSON sidecar.**
The bot can be looking at a perfectly fresh order book and still be forbidden to trade because a
cron job missed two slots. That is the actual cadence bug in Earn.

---

## The cadence I would set

**Leave the trading cadence exactly as it is.** Three specific non-changes, each with the number
behind it:

1. **Do not go faster than 4h.** 1h evaluation is −0.75 pp/yr and improves 15 of 31 pairs. An
   intrabar trigger is −1.7 to −2.4 pp/yr, doubles round trips and doubles fee drag. Measured,
   costed, refuted.
2. **Do not chase reaction speed.** Acting instantly at the daily close is *worse* than the
   shipped 4-hour delay by +0.26 pp mean CAGR and +0.007 Sharpe, and delay is free out to 48
   hours. The 28-hour worst-case latency in §1.3 is not costing money. If the owner wants the
   system to feel faster, that is a reporting change, not a trading change.
3. **Do not "fix" the 4h-vs-1d mismatch.** SleeveA is a daily strategy on a 4-hour clock, which
   is architecturally untidy, but the measured difference between acting at the daily close and
   acting 4 hours later is +0.26 pp with a 16/31 win rate — inside this study's ±2 pp noise floor.
   Changing `trading.timeframe` to `1d` would be a cosmetic change sold as an improvement. Refuse it.

**The one change I would argue for is on the permission side, and it is tier 2 (human only).**
`book_snapshots` is a blocking feed with no grace period, written by a 15-minute cron, checked
against a 30-minute limit. That is 30 minutes of headroom on the thing that decides whether the
book may be entered at all — and every minute of block is a minute in which the live evidence says
mechanical profit-taking (10 trades, +85.27, 10/10 won) cannot run. Three options, in the order I
would try them, for a human to weigh:

- **Decouple the freshness stamp from the 15-minute ingest job.** The bot already holds a live
  order book every 5 seconds; let the thing that *has* the data stamp its own freshness rather
  than inheriting a cron job's heartbeat.
- **Or widen the book-snapshot grace to one cadence period** (15 min), matching the treatment
  `candles_<tf>` already gets, so a single missed cron slot cannot approach the limit. This is a
  safety property, so it is a human's call, not a tier-1 tweak.
- **Or write book snapshots more often than ingest runs.** Cheapest in code, least satisfying.

And one operational rule that follows directly from the 2026-09-30 evidence: **never run heavy
read-copies against the live databases while the bots trade.** Two of this month's three entry
blackouts trace to write-lock contention or a watchdog that stopped, not to the market.

## Trials, and what this study does *not* clear

| Item | Value |
|---|---|
| Book-variants measured in this study | **76** (9 multi-grid books + 11 lag levels + 14 leak-free cadence × signal + 28 in two discarded buggy runs, counted honestly + 14 re-runs) |
| Cumulative selection trials | 8,144 → **~8,220** |
| Effect on the deflated hurdle | `√(2 ln N)` moves 4.243 → 4.245; the hurdle stays **~2.32** |
| Best Sharpe any cadence variant reached | **0.834** (slow signal, look every 168 h, equal-weight portfolio) |
| Same-universe buy-and-hold | **0.841** |
| BTC-hold arithmetic baseline | **0.83** |
| Clears the hurdle? | **No — by a factor of ~2.8.** Nothing in Track 4 is a shippable edge |

Track 4 produces **no change proposal**. Its output is a refutation of three plausible-sounding
improvements and one concrete operational finding about the permission pipeline.

## Limits of this work

- The live `knowledge/earn.db` and `journal/journal.db` were never opened. Everything about the
  two real blackouts comes from code comments in the working copy and from the relayed context,
  not from `ingest_runs` or `gate_decisions`. The **realised** rate at which the staleness gate
  has blocked entries is therefore **unmeasured** — I bounded it from the data's own gaps only,
  and the pipeline failures the bound cannot see are the ones that actually happened.
- The 31-pair whitelist is **survivorship-selected**: these are the names that made today's
  whitelist, which is why the same-universe buy-and-hold reaches 41% CAGR. That inflates every
  hold baseline here. It does not affect the *paired* cadence comparisons, which are the findings.
- Feather data ends **2026-09-23** (`backtest_data` runs Sunday 18:00), so the last week is absent.
- SleeveB was not modelled. Its behaviour depends on proposal content, which is not reconstructible
  from candles. Everything above models SleeveA's rule.
- The hypothesis seal lives in the scratchpad, not `knowledge/state/hypotheses/`, so it is not in
  the repo's own audit trail. A human should re-seal it there if these results are ever cited.
- The trial count is my own honest tally; `audit_stats.py trials --add` was not run, because it
  writes to `knowledge/state/`.
- The ±2 pp noise floor I measured on median-across-31 CAGR means several rows in the H1 table
  (12h, 24h, 336h) should be read as "no different from the shipped cadence", not as small effects.
