# Coins that go up hundreds of percent, where to sell them, and what the model layer is doing

Status: measurement only, written 2026-10-01. **Nothing here is switched on by this document.**
No bot, cron, unit or console was touched; no live database was opened (all operational numbers
come from the read-only snapshots at `~/pnlsnap/db/`, taken 2026-10-01T18:36Z); no venue was
called; `.env` was never read. Six measurement passes (M1-M4, A1-A2) were each attacked by an
independent adversarial pass, and **every number below is the post-attack number.** Where an
attacker corrected a figure, the correction is what appears here and the original is shown beside
it so the record is honest.

---

## The two answers, up front

**1. "Can we spot the coins that go up hundreds of percent, and sell them at the right low?"**
**No — and the reason is the mandate, not a detection gap.** 84.4% of the real coins that ever
traded against USDT on Binance doubled inside 90 days at some point (515 of 610, across 1,843
distinct episodes). Only **4.18% of those doublings happened in a coin Earn was allowed to buy**
(77 of 1,843). The median participant in a doubling was worth **88.7% less** at its last print
than at the episode peak, and **39.1% of those tickers stopped printing altogether** (against
16.8% of the non-movers). Every ordering built on top of the mover population loses to simply
holding BTC after costs: the best "chase the doublings" book inside Earn's eligible set returns
**−25.2%/yr at a −99.3% drawdown** against BTC's +27.5%/yr at −76.6%.

Worse for the idea: the features that predict a *good forward return* predict a *lower chance of
a 2x*, on all five features without exception. The 2x objective and the money objective point in
opposite directions. There is no version of this that both reaches the doublings and makes money.

**2. "At what low should a doubled position be sold?"**
**There is no give-back number worth shipping, and the one that nearly shipped is worse than what
Earn already runs.** The study that produced "sell on a 15% give-back from the running high"
compared it against the 200-day MA flip *alone* — but the shipped strategy also arms a hard
**−10% stop from entry**, and that stop fires before the MA flip in **85.0%** of the very episodes
the study measured. Against the exit that actually ships, the 15% give-back measures **−2.02
percentage points per episode (t −0.53)** — it loses. It also loses by 1.21pp to an
information-free "sell after 30 days no matter what".

The level that already governs is the right answer to the owner's question, and it is already in
the code: **−10% from entry** (`config/earn.yaml:263` `fixed_pct: 0.10`, clamped by
`risk.stoploss_per_trade` and set as Freqtrade's static stop at `strategies/earn_base.py:195` via
`strategies/mechanics.py:292-295`; the repo's own test asserts it at
`tests/strategies/test_adapter.py:68`).

**3. "Is the Claude / local-model routing well optimised?"**
**Partly. The local tier is excellent and the cost discipline is not.** `granite4.2:3b` served
232 of 232 local calls with zero failures, including 208 holdings checks at $0.00 and 1.6 s mean;
77.9% of all input tokens are served from prompt cache; and the code-level floors that keep a
cheap model out of the decision seat are real and exercised. But the layer ran at **$217.70 per
30 days against a stated $150 total**, nothing throttles it, **$7.73 of the $17.79 ever spent on
deciding bought the words "hold, confidence 0.50"**, and three of the twelve configured tasks have
never successfully run. Four specific defects, all measured, are listed in §7.

---

## 1. The movers question — REFUTED

Source: `~/earn-panels/panel_1d.parquet` (survivorship-free; dead tickers retained;
2017-08-17 to 2026-09-24). Episode = first trigger day of a cluster where
`close / trailing-90d-min >= 2.00`, clusters split at gaps >= 90 days, peak = the cluster max.
Both the original pass and the independent attack computed this from the same file with separate
scripts; the figures below are the ones that reproduced.

### 1.1 The population is real and large

| measure | value | baseline |
|---|---|---|
| Episodes of +100% off a trailing 90-day low | **1,843** | — (census, not a trial) |
| Distinct tickers | 557 of 747 symbols; **515 of 610 real coins (84.4%)** | — |
| +200% in 90 days | 828 episodes / 423 tickers | — |
| +500% in 90 days | 277 episodes / 223 tickers (181 in 2021 alone) | — |
| Per year | 2019: 33 · 2020: 161 · 2021: 300 · 2022: 200 · 2023: 360 · 2024: 415 · 2025: 208 · 2026: 160 | — |

**Correction carried from the attack:** "747 coins" is wrong. 137 of the 747 symbols (18.3%) are
not coins by Earn's own funnel — 48 Binance leveraged tokens (`ops/universe.py:405`
`_is_leveraged`), 68 instruments that fail the repo's 24/7 weekend-volume test (tokenized
equities, `ops/universe.py:302-320`), 21 pegs and fiat, 2 tokenized gold. 70 of the 1,843 episodes
are those non-coins, most of them leveraged tokens that "double" when the underlying moves ~26%.
The corrected denominator (515 of 610 real coins = 84.4%) makes the finding *stronger*, not
weaker.

### 1.2 What happened to the participants

| measure | value | baseline |
|---|---|---|
| Median return from the episode peak to the ticker's last print | **−88.7%** | 0% break-even; BTC hold +27.5%/yr over the span |
| Ended more than 50% below the peak | 84.1% of episodes | — |
| Ended more than 90% below the peak | 46.4% | — |
| Still above the peak at last print | 4.8% | — |
| Tickers with no print in the last 30 days of the panel ("delisted") | **39.1%** of movers vs **16.8%** of non-movers | 0% for BTC/ETH |
| Median return from the peak, +90 days forward | −53.8% | 0% |

**Two corrections the attack forced, both reported rather than buried:**

* **−88.7% is not caused by the doubling.** The same tickers, same last-print convention,
  anchored on 90-day highs at a fixed calendar offset with no episode involved, give a median of
  **−82.9%**. Only about 6pp of the 88.7 is attributable to the doubling event. The statistic
  mostly measures "a running maximum versus a terminal price across nine years of USDT alt
  listings". The genuinely mover-specific part is the delisting rate (39.1% vs 16.8%).
* **The "highlight-reel gap" number does not reproduce.** The original pass reported a mean
  peak-to-last of +236.6% against the −88.7% median. Independent re-derivation gives **+103.8%**
  (+110.7% on real coins only). The sign and the mean-versus-median gap hold; the number does not.
  Use +103.8%.

### 1.3 The mandate is what removes 95.8% of them

Earn's point-in-time eligibility funnel was rebuilt day by day from `config/earn.yaml:88-100`
(`universe.rules`), `:102-103` (tiers) and `:110-117` (`satellite_eligibility`) using the repo's
own metric definitions in `ops/universe.py`, and then **validated against the stored universe
snapshot `knowledge/universe/2026-09-23.json`: core 2/2, major 6/6, satellite 23/23, tradeable
31/31, Jaccard 1.000.** That validation is why this number can be trusted.

| measure | value |
|---|---|
| Episodes in a coin that passed Earn's eligibility that day | **4.18%** (77 of 1,843), 34 distinct tickers |
| Tier split of those 77 | 47 major · 21 satellite_eligible · 9 core (BTC/ETH) |
| Admitted by the looser *watchlist* gate | 53.7% |
| Comparable figure for +30% rallies (measured 2026-09-30) | 2.2% |

So the funnel that removes 95.8% of the doublings is the **tier / eligibility layer**, not the
watchlist, and that is a deliberate, previously measured choice (`docs/design/growth-audit.md`).
The pre-registered falsifier was 20% — nowhere near reached.

### 1.4 Reach is not the constraint, and the two objectives fight each other

* **One in ten eligible names doubles per quarter.** 10.4% of Earn's eligible set touches a 2x
  within any 90 days (mean breadth 25.0 names); 17.2% on the full panel. Earn's universe is not
  starved of doubling candidates.
* **The features that rank forward returns are the ones already shipped.** Rank IC against forward
  90-day return, Newey-West t (3 lags), 101 monthly dates, Earn-tradeable universe: `vol60`
  **−0.269** (t −5.48), `age_days` **+0.190** (t +3.48), `log_adv90` **+0.100** (t +2.66), against
  a break-even rank IC of **0.0264**. All three clear it — so that pre-registered falsifier fired,
  and it is reported. But all three say *buy old, low-volatility, liquid*, which is precisely what
  `universe.satellite_eligibility` already encodes (`age >= 1095d`, `vol60 <= 1.00`,
  `qv90 >= $10M`, `config/earn.yaml:110-117`). This is an out-of-sample confirmation of a shipped
  filter on a survivorship-free panel — a positive result about existing code, not a new edge.
* **The inversion, which kills the idea outright.** The cross-sectional AUC for "will this double
  in 90 days" runs the *opposite* way to the return IC on all five features. `vol60` AUC 0.568
  while its return IC is strongly negative; `dist_ath` 0.427 (0.341 tradeable); `age_days` 0.484
  (0.371); `log_adv90` 0.444; `dd365` 0.439 — against 0.50 for no skill. **Every feature that
  predicts a good return predicts a lower chance of a 2x, and the one that predicts a 2x predicts
  a worse return.**
* **BTC regime adds ~2pp and nothing more.** 2x base rate 18.1% in BTC up-regime vs 16.1% down
  (11.5% vs 9.1% tradeable). Real, far too small to trade.

### 1.5 The costed books, all net of 0.30% per round trip

Arithmetic Sharpe (mean/std × √365) on daily returns, 2018-01-31..2026-09-24 (8.65 y, 3,159 days),
monthly rebalance, 0.30% charged on turnover at every tranche formation.

| book | net Sharpe | CAGR | max DD |
|---|---|---|---|
| **BTC buy-and-hold (baseline, same span)** | **0.705** | **+27.5%** | **−76.6%** |
| Top-3 lowest `vol60`, eligible set — best tradeable book | 0.791 | +31.7% | −67.0% |
| Hold the whole eligible set equal-weight (control) | 0.576 | — | — |
| Moonshot: top `vol60` decile, eligible set | 0.032 | **−25.2%** | **−99.3%** |
| Moonshot: far-below-ATH decile, eligible set | 0.170 | −14.4% | −95.0% |
| Top `vol60` decile, full 747-symbol panel (**untradeable**) | 0.964 | liquidity fiction | −93.1% |

The deflated hurdle is **2.98** at 8,457 cumulative trials (**2.364** recomputed at this 8.65-year
sample with the repo's own formula, `runs/features/sampling.py:291`). **Nothing measured clears
either.** The best tradeable book beats BTC but misses the hurdle by 2.19 Sharpe — and it is not
a new idea: "hold the three lowest-vol eligible names" is the existing eligibility filter with a
top-3 cut. The full-panel 0.964 is reported only as an untradeable upper bound; its printed CAGR
requires buying an equal-weight basket of the highest-volatility microcaps monthly at 15 bps/side
with no market impact, and 95.8% of those names Earn may not buy. A real impact model makes those
numbers **worse**, never better, so the negative is safe in that direction.

**What the measurement was worth.** Three things: the eligibility filter is confirmed out-of-sample
on a survivorship-free panel; the case for *not* loosening it to reach movers is now quantified
(4.18% reachable, and the reachable fraction barely rises with the size of the move: 3.74% at
+200%, 4.36% at +500%); and the 2x-versus-return inversion means this family can be closed rather
than revisited.

---

## 2. Where to sell after a double — REFUTED, with the honest number

Sample: 173 non-overlapping post-double episodes (`close/close[-90] >= 2.00`, first such day in
180 bars, tradeable that day) from 85 symbols, 2018-02-13..2026-09-03, 180-bar window, daily
closes, entry held fixed and dumb for every rule so the sweep measures the exit alone, 0.30%
charged on the exit leg. 42 exit rules swept, pre-registered. The attacking pass reproduced the
implementation exactly (173 episodes, 85 symbols, chandelier +3.52%, control −5.07%, MA200 −7.11%,
sign test 67/82 p = 5.26e-09), so what follows is not an implementation dispute.

### 2.1 The baseline was wrong, and fixing it reverses the result

The study defined "the exit Earn ships today" as `close < SMA200` alone. The shipped strategy also
arms Freqtrade's **static −10% stop**, unconditionally: `strategies/mechanics.py:292-295`
`effective_fixed_stop` returns `−min(|fixed_pct|, |ceiling|) = −min(0.10, 0.15) = −0.10`, assigned
at `strategies/earn_base.py:195`, from `config/earn.yaml:263` and `risk.stoploss_per_trade`. The
`use_custom_stoploss` flag at `earn_base.py:197-200` only gates the *callback that could override*
that stop — it does not make the stop dark. **On the study's own 173 episodes the −10% stop fires
before the MA200 flip in 85.0% of them**, so the study measured a rule that governs 15% of its
sample.

| comparison, mean net return per episode | value | baseline |
|---|---|---|
| **The real shipped exit (−10% stop + MA200 flip)** | **+2.30%** — 3rd of 44 series | do-nothing control −5.07% |
| The same, pessimistic intrabar-fill convention | −1.85% — 30th of 44 | — |
| MA200 flip *alone* (the strawman the study used) | −7.11% | — |
| Chandelier 3×ATR(14) **vs the real shipped exit** | **+1.21pp, t 0.27, 44 of 72 symbols, p 0.076**, bootstrap 95% [−1.49, +11.68] | zero |
| the same, intrabar convention | +5.37pp, t 1.53, 46 of 82, p 0.32 | zero |
| **15% give-back vs the real shipped exit** | **−2.02pp, t −0.53**, 39 of 66 symbols | zero |
| "Sell after 30 days, no matter what" (information-free placebo) | +1.50% — 4th of 43; beats the strawman by +8.62pp (p 3.3e-06) | — |

The headline the study published — chandelier beats the shipped exit by **+10.63pp on 67 of 82
symbols, p = 5.3e-09** — becomes **+1.21pp, p = 0.076** once the baseline includes the stop that
is actually armed. And **81% of the apparent effect is reproduced by an exit that contains no
information at all** (sell on day 30). The mechanism was "exit sooner on a population that drifts
down", not stop-rule design.

### 2.2 So what is the give-back number?

**There is none to ship.** For completeness, because the owner asked for a number with a range:

* **The plateau is wide and flat.** All eight pre-registered give-back cells (5/8/10/15/20/25/30/40%
  from the running high) beat the *do-nothing control* by +0.15pp to +5.36pp. The spread from X=5
  to X=30 is **1.2pp against a bootstrap CI roughly 20pp wide** — so X is not identified beyond
  "somewhere in 10-25, and not 40". It is a plateau, not a peak, and the plateau is wider than the
  effect.
* **Even the best cell is not statistically established.** 15% vs the control is +5.36pp with
  t_clustered **1.01** and a 95% CI of **[−4.77pp, +15.57pp]** that straddles zero. The best t
  anywhere in the 42-cell grid is 2.32, below the multiplicity threshold √(2 ln 42) = **2.734**
  that the study itself pre-registered.
* **And against the real baseline the whole family loses** (−2.02pp, t −0.53), which makes the
  plateau moot.
* **The level that already governs is −10% from entry**, and it is already shipped and tested.

Two further defects in the winning cell, for the record: its stop (`HH22[t-1] − k·ATR[t-1]`) is
unanchored to the entry and not tighter-only, so it **sat above the entry close in 12.1% of
episodes and exited on bar 1 in 15.6%** — it silently vetoed entries, contradicting the study's
own "entry held fixed" claim. And the sweep never modelled partial exits
(`m2_sweep.py:239-249`), which is why the ladder cell and the control carry *byte-identical*
Sharpe (0.3049982995109163) and max drawdown; every partial-exit Sharpe in that table is wrong and
the column is confounded with time-in-market. Nothing in the sweep came within 2.4 Sharpe of the
2.98 hurdle, and no cell beat BTC buy-and-hold (0.827 on that sample).

### 2.3 What the exit measurement WAS worth — the conditional numbers

These survived everything and are the genuinely useful output. Once a *tradeable* name has doubled
in 90 days:

| measure | value |
|---|---|
| Further upside to the eventual peak | median **+31.2%**, p25 +8.7% |
| Share of cases with less than +10% left to the peak | **26.0%** |
| Holding 180 more days from the double | median **−36.9%** |
| P(loss) over those 180 days | **71.1%** |
| P(worse than −50%) | **32.4%** |

Read plainly: after a double, the *remaining* run is usually small and the downside is large. That
argues for **booking profit mechanically** — a take-profit ladder — far more than it argues for any
trailing stop. See §5 for why that is the one thing worth measuring next, and note that it has
**not** been measured.

---

## 3. What the live exit record actually is (and why it proves less than it looks)

The standing context says the live record shows the exit side losing: `roi` +
`trailing_stop_loss` = 10 trades, 10 wins, +85.26 USDT; `exit_signal` = 12 trades, 0 wins,
−73.73 USDT (refreshed from the 2026-10-01T18:36Z snapshot; the earlier handover said 9/+85.26 and
10/−73.10, and the winners' P&L matches to the cent). **Three findings mean this record cannot
corroborate anything about the shipped strategy:**

1. **It is a different strategy on a different timeframe.** `ft_a_run.db` / `ft_b_run.db` carry
   `strategy=SleeveFast`, `timeframe=60`. The committed tree runs SleeveA/SleeveB at 4h
   (`config/earn.yaml:254`) with `profiles.active: null` (`config/earn.yaml:620` — verified in this
   document's own read of the committed file). The 4h sleeves have **4 closed trades in total**
   (`ft_a_old`/`ft_b_old`, both 2026-09-23, exiting by `force_exit` −42.21 and `target_zero`
   −27.56). So 22 of the 26 closed trades, and every `exit_signal` loss, come from a 1-hour
   plumbing profile. Whether that record says anything about the shipped 4h MA200 exit
   (`strategies/SleeveA.py:94`) is **not established**.
2. **Every trailing win bar one had a peak under +2%.** The observed trailing-exit peaks are
   1.5547%, 1.9255% / 1.9622% (ONDO, both sleeves), 1.8415%, 1.7917% / 1.8114%. A 10-for-10 record
   on sub-2% peaks is a plumbing observation, not an edge.
3. **The one double-digit trade says the opposite of what was reported.** AVAX/USDT
   (`ft_a_run.db` id=6): open 10.627, `max_rate` 11.743 (peak +10.502%), close 11.730,
   `exit_reason=roi`, `close_profit` 0.048007. The claim that it "handed back 5.70pp, 54% of its
   peak" is an accounting artefact. The orders table shows **three** exit legs: 29.98% of the
   position at +0.894% and a further 28.01% at +1.364%, both inside the first 11 minutes — those
   are the fast-test take-profit **ladder** rungs — and the remaining 42.01% at +10.379%.
   Decomposed: **5.49pp is ladder dilution, 0.21pp is fees, and 0.05pp is the only genuine
   give-back.** The exit that fired sold **0.111% below the trade's high-water mark** — it captured
   98.9% of the peak on the tranche it closed. And the armed trailing stop for that trade was
   recorded at 11.682, i.e. **0.41% below the 11.730 the ROI table actually got**. On the single
   most favourable case a give-back exit could be handed, the give-back exit would have booked
   *less*.

The useful conclusion from the live record is therefore narrow and real: **a mechanical,
price-anchored exit captures what it aims at, and the ladder rungs dominate the outcome far more
than the stop rule does.**

---

## 4. The model layer: what runs, when, on what, and what it cost

Window: the journal's entire history, 2026-09-23T13:37:36Z to 2026-10-01T18:35:22Z = **8.21 days**
(a 10-hour host suspension sits inside it, so per-day rates are soft; totals are exact). Dollars
are the SDK's reported `cost_usd`, which under the Claude Max subscription is **notional** — 0 of
436 `llm_calls` rows carry `auth_source='api_key'` and metered spend is $0.0000. The real binding
resource is the rate limit (0.94 utilization at 2026-09-30T16:05:53Z).

### 4.1 The table

| task | chain (effort) | what calls it, how often | declared $/mo | measured in 8.21 days |
|---|---|---|---|---|
| `extract` | haiku → local_small (low) | **no caller anywhere** (`runs/router.py:73` is the only non-test mention) | 4 | 0 calls |
| `classify` | haiku → local_small (low) | `runs/ingest.py:737`, every 15 min (`ops/crontab:31`) | 5 | 114 calls, 108 ok, **$2.80**; 101 on haiku vs 7 local |
| `holdings_watch` | **local_small** → haiku (low) | `runs/watch`, every 7 min (`ops/crontab:33`); `local_only: true` (`config/earn.yaml:407`) | 4 | **208 calls, 208 ok, $0.00**, 1.6 s mean |
| `scan` | **local_small** → haiku (low) | `runs/signals/screener.py`, every 5 min (`ops/crontab:32`) | 3 | 80 calls, 62 ok, **$4.86**; 34 context skips, 8 gray-zone cloud calls |
| `flags` | haiku → local_small (low) | `runs/research_run.py:166`, 2×/day | 4 | on the SDK ledger; see note |
| `brief` | sonnet → haiku → local (medium) | `runs/research_run.py:198`, 2×/day | 12 | 5 runs; **2 of 5 served haiku when sonnet was requested**, no switch row; best cache ratio (6.05) |
| `validate` | sonnet → opus (high) | `runs/signals/validator.py`, max 6/day (`config/earn.yaml:380`) | 30 | 31 validations, **$15.66**, **18 errored ($8.62)**; opus never reached; panel never ran |
| `discover` | sonnet → opus (high) | `runs/discovery.py:854-860`, daily 02:20 + Sat 04:00 (`ops/crontab:43-44`) | 35 | 6 runs, **5 failed**, $0.17 |
| `decide` | **opus (max)**, escalation **fable** | `runs/research_run.py:272`, 2×/day 08:30 + 16:00 Gulf (`ops/crontab:40-41`) | 60 | 12 runs, **$17.79**; **11 served fable, escalated=1 on every one** |
| `adjudicate` | fable → opus (max) | **no caller anywhere** (`runs/router.py:64` only) | 25 | 0 calls |
| `review` | fable → opus (max) | `runs/review_run.py`, Sun 20:00 (`ops/crontab:45`) | 70 | 1 run, **failed**, $4.88, produced a 241-byte stub |
| `daily_review` | fable → opus (max) | `runs/daily_review.py`, 21:30 daily (`ops/crontab:42`) | 40 | 3 runs, **all journaled failed**, $10.71 — but 3 real reports exist on disk |

*Note on `flags` and `brief`:* both run on the SDK path, whose ledger is the `runs` table.
Of its $35.6478 total, `decide` is $17.7939, `daily_review` $10.7067 and `review` $4.8794, leaving
**about $2.27 across the remaining 27 rows** (brief, flags, discover). No pass isolated that split
further.

**The declared per-task budgets sum to $292** (4+5+4+3+4+12+30+35+60+25+70+40) against
`budget.monthly_total_usd: 150` at `config/models.yaml:426`. They are also inert: `runs/llm/chain.py:559`
returns no block unless `budget.mode == "hard"`, and `config/models.yaml:425` sets `telemetry`.
The one cap that *is* live regardless of mode is `auth.api_key_monthly_cap_usd: 30`
(`config/models.yaml:75`, checked at `chain.py:552-558`) — and it has never bound because metered
spend is $0.

### 4.2 Total spend, with the denominator stated

**$59.5616** over 8.21 days = **$217.70 per 30 days** against the stated $150 total (**1.45×**).
The two ledgers reconcile without double-counting: `llm_calls` $23.9138 (436 rows) + `runs`
$35.6478 (43 rows); the only tasks in both are `brief` and `discover`, two `llm_calls` rows
totalling $0.5945 whose parent `runs` row carries $0.0000.

*This denominator matters and was got wrong four different ways.* The same $59.5616 was published
as $255/30d, $221, $199 and $193 across five passes purely by choice of denominator (7 "active"
days, 8.21 journal days, 9.21 calendar days). **8.21 days is the journal span; $217.70/30d is the
figure.** Likewise `decide` measures **$65/month** against its own $60 cap on the full window — but
the last 24 hours of the window (4 runs, $9.63) imply **$289/month**. Both are reported because the
cost per decide run grew roughly 6× across the window ($0.43 → $2.69 as output grew 5,910 → 28,070
tokens) and it is not known whether that is still climbing or has plateaued.

### 4.3 What is genuinely well optimised

* **The local tier works.** `granite4.2:3b` is **232 of 232 ok** — 208 `holdings_watch` at 1,613 ms
  mean and 24 `scan` at 12.0 s — at **$0.00**. Every local failure in the journal belongs to a
  **retired** model (`llama3.1:8b` 3 timeouts, `qwen3.5:4b` 3 timeouts), not to the model now
  configured. `think: false` is set unconditionally at `runs/llm/providers/ollama.py:149`, which is
  what stopped local calls returning empty content with a full `thinking` field.
* **Prompt caching is excellent.** 77.9% of input tokens are served from cache; read:write 3.54
  overall (brief 6.05, decide 2.67, flags 2.86).
* **Deterministic-first holds.** 86 of 302 `watch_events` were numeric invalidations that called
  **no model at all** (`model_alias` NULL, by design at `config/earn.yaml:392-394`), and the
  watcher raised 30 hand-raises without touching an order — `runs/watch/guard.py` enforces that
  rather than asking for it.
* **The code floors are real.** `MIN_TIER_FLOOR {decide: 4, validate: 3}`
  (`runs/llm/types.py:159`), `ALWAYS_LOCAL_FORBIDDEN {decide}` (`:162`), `EFFORT_FLOOR = "high"`
  (`runs/router.py:45`), re-checked on the alias actually chosen rather than only on the chain
  (`runs/router.py:354-360`, which raises rather than downgrading). **No local model served
  `decide` or `validate` in 436 calls.** `config/models-auto.yaml` does not exist, so no tier-1
  overlay is active and `shadow.enabled` is false (`config/models.yaml:430`) — the human-pinned
  base is fully in effect.
* **No silent substitution on the chain path.** Every drop writes a `provider_switches` row
  (`runs/llm/chain.py:309, 328, 336`). The one exception found is on the SDK path (see §4.4 item 7).
* **The one apparent tier-floor breach is not one.** The 2026-09-23T16:52Z decide run is journaled
  `served_model=claude-haiku-4-5-20251001` against a tier-4 floor. That string was written by code
  the repo itself documents as mis-attributing Opus as Haiku
  (`runs/decision_core.py:110-126`: *"a run decided by Opus was routinely journalled as served by
  Haiku"*), and the fix landed 3 h 28 m **after** that run. The row's own cost arithmetic settles
  it: in 8 / out 5,910 / cache-read 67,065 / cache-write 25,112 at $0.434776 implies Opus-5 list
  prices to within **1.29×** — dead centre of the 1.16-1.44× band every uncontested row in the
  journal shows — while Haiku implies **6.43×**, four and a half times outside it. **Opus has served
  `decide` exactly once: the one run that was not escalated.**

### 4.4 The misroutings, ranked by measured dollars

1. **`decide` escalates on plumbing, on essentially every run.** 11 of 12 runs served
   `claude-fable-5-1` with `escalated=1`, at **$1.5781/run against $0.4348** for the one that was
   not, and **$7.7267 (43.4% of the $17.7939 decide ledger) bought seven proposals of
   `abstain=1, confidence 0.50, module "hold", targets {USDT: 1.0}`**. The mechanism is
   `runs/router.py:350`: *any* of six hard-case flags escalates, with no hysteresis. `two_abstains`
   (`runs/router.py:303-310`) reads the last two valid non-shadow proposals, so a run of abstentions
   **self-escalates** — proposals 1-8 (2026-09-23T16:52Z..2026-09-29T13:14Z) were all
   `abstain=1, module=hold`. It was the sole escalation reason on 5 runs ($5.99); the live cause on
   the last 4 runs ($9.63) is `regime_change_48h`, which the system's own rationale calls "the
   unknown→trend_up recovery artefact, not a flip". **A unanimous abstention is a consensus, not a
   disagreement; paying the most expensive model in the matrix to break a tie between two
   abstentions is the clearest waste in the layer.**
   *Not a paper-versus-practice gap.* `config/models.yaml:332` declares `escalation: fable` for
   `decide`, `:34` lists the row as "decide / opus / max / fable @ max (panel)", and `:360` names
   fable the panel adjudicator. The config got exactly what it asked for, through
   `runs/router.py:353`. **The defect is the trigger rate, not the matrix** — which matters,
   because "write fable into `tasks.decide.chain`" would edit the wrong object and change nothing.
2. **`daily_review` dies on its turn cap, and is journaled as a failure even when it succeeds.**
   3 runs, $10.7067, all `status=failed`; two died on *"Reached maximum number of turns (40)"* at
   **41 and 57 turns** against `max_turns: 40` — declared **twice**, at `config/models.yaml:409`
   and `config/earn.yaml:570`. `models.yaml` already documents this exact failure and fixed it for
   `scan` (2→4) and `classify` (1→2), with the principle written out at `config/models.yaml:198-203`:
   *"a turn cap below what the answer needs does not save money, it spends it and discards the
   result."* **But the money was not all wasted:** `reports/daily/2026-09-24.md` (5,597 B),
   `2026-09-28.md` (8,713 B) and `2026-09-29.md` (8,397 B) each open with model-authored narrative
   naming their own `run_id`, with grading packs of 6,275 / 13,717 / 12,578 B beside them, and each
   file's mtime equals its run's `finished_utc` to the second. The stub test is decisive: only
   `reports/review-2026-W40.md` (241 B) carries the "session produced no report" line. The reason
   all three are journaled failed is `runs/daily_review.py:175-182`: `_session_outputs_ok` demands
   a `decision_grades` row, and `decision_grades` is **empty**. **A complete review is being marked
   failed on a grading-table check.**
3. **`validate`'s citation allowlist rejects more than half the verdicts it pays for.** 18 of 31
   validations errored (**$8.6240**), **15 of them for "unknown feature_key"**, and the key names
   changed after each fix (`market_state.*`, then `watchlist.*`/`portfolio.*`) — the signature of a
   hand-maintained list drifting from its source. Of *paid* verdicts the rejection rate is **17 of
   30 = 56.7%**. Each rejection also kills a signal (`signals.status='error'`, 18 rows, one for
   one). It is live: the most recent rejection is 2026-09-30T20:00:21Z. **It is not silent** — every
   rejection is journaled verbatim in `signal_validations.error` and mirrored into
   `signals.status_reason`, and the console reads both (`console/services/signals_service.py:103`,
   `:130`). What is quiet is only the `llm_calls` ledger, which marks the same calls `status='ok'`.
4. **`scan` is over its cap by design, and the obvious fix would not touch it.** The 34
   `skipped_capability` switches (prompt ~15.6k tokens against `max_ctx: 8192`,
   `config/models.yaml:109`) all fall on 2026-09-23..09-29 and **stopped after the lean-prompt
   switch** — since 09-30 granite serves every scan as chain head. But Claude still served 8 scan
   calls on 09-30/10-01, each in the **same** `run_ref` seconds after a successful granite call:
   those are the designed **gray-zone second opinion** (`config/earn.yaml:374`
   `gray_zone: [0.45, 0.65]`; `config/models.yaml:184-193`), with no switch row. That path alone
   runs ~**$10.6/month against scan's $3 cap**. Raising `num_ctx` would not remove a single one of
   them.
5. **Two ledgers, and the console shows one.** `llm_calls` carries no `decide`, `flags`, `review`
   or `daily_review` rows; `console/services/llm_service.py:333` reads `llm_calls` only, so the
   usage page shows about 40% of spend and **none of the most expensive task**. `llm_calls` also
   has no cache columns, so its `input_tokens` reads ~10 per Claude call against ~1,223 per Ollama
   call — prompt volume is invisible there.
6. **Declared config that cannot execute.** Both panels are `enabled: true`
   (`config/models.yaml:283` validate, `:344` decide) but `run_panel` (`runs/llm/panel.py:315`) has
   **no caller outside tests** — so the cross-effort vote the matrix calls "the cheap one, so it is
   the one that always runs" has produced **zero** measurements in 12 decide runs and 31
   validations. `extract` and `adjudicate` have no callers at all. `xhigh` is defined in
   `EFFORT_ORDER` (`runs/router.py:44`) and used by **no task** — so authoring tasks escalate
   `high → max` and skip the rung vendor guidance calls the sweet spot for agentic work.
7. **Two small things worth one look each.** `brief` requested sonnet and was served haiku on 2 of
   5 successful runs (including the latest, 2026-10-01T04:30Z) with **no `provider_switches` row**,
   against `config/models.yaml:13-14`'s claim that "there is no silent substitution anywhere in this
   system" — n=2, so this is a lead, not a finding. And `haiku` is the matrix's only **dated** pin
   (`claude-haiku-4-5-20251001`, `config/models.yaml:93`) where the canonical id is
   `claude-haiku-4-5`; that snapshot **is** still being served (135 successful calls, latest
   2026-10-01T18:35:22Z), so this is hygiene, not an outage.
8. **`max_ctx` is declared for one model only.** `config/models.yaml:109` declares it for
   `local_small`; `runs/llm/chain.py:128-137` treats `max_ctx is None` as "always fits" — and its
   docstring says so deliberately: *"the Claude models do not declare one here, and inventing a
   number for them would be worse than not checking."* The real windows are knowable (Haiku 4.5 is
   200K; Opus 5, Sonnet 5 and Fable 5.1 are 1M), so declaring them would convert a documented
   non-check into a check. **Low value today** — no prompt in the record came within an order of
   magnitude of 200K — which is why it is not in the build list.

**Corrected waste figure.** An earlier pass put "42.9% of all spend bought nothing". With the three
daily reports credited to the runs that produced them, the honest figure is **25.0% of $59.5616**
(upper bound 37.4% on the reading most generous to the original claim). Both are reported because
the true denominator is itself a lower bound: `runs/daily_review.py:446-459` records only the
**last** attempt, so the primary fable attempts that authored two of those three reports appear in
no ledger at all.

---

## 5. The one thing worth measuring next

**A take-profit ladder on 4h bars, with partial exits actually modelled.** The reasoning is in
§2.3: after a double, median remaining upside is +31.2% but 26% of the time less than +10% is left,
and holding on gives a median −36.9% with a 71.1% chance of loss. That is an argument for booking
profit in slices, not for a trailing stop. The lever is config-only and currently empty:
`take_profit.ladder: []` at `config/earn.yaml:271`. Rungs at, say, +15/+30/+60% clear the
`min_edge` floor trivially — `risk.min_edge` is `{round_trip_cost_pct: 0.0030, multiple: 3.0}`
(`config/earn.yaml:246`), i.e. 0.90% gross on the **smallest** booked target, and the gate refuses
every entry below it (`strategies/riskgate.py:457` `min_booked_target`, `:480` `min_edge_ok`), so a rung at
+0.5% would halt all entries while a rung at +15% is free.

**It is unmeasured, and must not be shipped on this document's authority.** The exit sweep's ladder
cells are invalid (§2.2: partial exits were never modelled, and the ladder cell's Sharpe is
byte-identical to the control's). Separately, the live AVAX trade (§3) shows the ladder dominating
the outcome — 5.49pp of a 5.70pp gap — which is exactly why it deserves a real measurement rather
than a guess. Any such study must run on 4h bars from `data/binance/*-4h.feather`, because the
sleeves trade `trading.timeframe: "4h"` (`config/earn.yaml:254`) and everything measured so far is
daily.

---

## 6. Trials added and the hurdle

| | trials | cumulative | deflated hurdle |
|---|---|---|---|
| Standing | — | 8,457 | 2.9800 |
| Mover census, precursors and books (§1) | **+23** | 8,480 | 2.9803 |
| Exit sweep, 42 cells (§2) | **+42** | 8,522 | ~**2.9805** |
| Every audit and adversarial pass (§3, §4) | 0 | 8,522 | ~2.9805 |
| **This document** | **0** | **8,522** | **~2.98** |

The hurdle is unchanged at two decimals: at 8,522 trials, marginal trials no longer matter. What
matters is that **the best measured candidate anywhere in this work is 0.964 Sharpe (untradeable)
and 0.791 (tradeable), against a hurdle of 2.98 and a BTC buy-and-hold baseline of 0.705 on the
8.65-year sample** (0.827-0.83 on the longer sample). Every number in §1 and §2 is net of 0.30%
per round trip, and risk-adjusted return is always the arithmetic Sharpe (mean/std × √365), never
CAGR/vol.

---

## 7. Build list, ranked

Ranked by measured dollars or measured defect, not by ambition. **Every row is tier 2 (human only)**
— there is no tier-1 route for any of it: `bounds:` at `config/earn.yaml:314-318` lists exactly four
keys (`sleeve_a.vol.target_annual`, `sleeve_a.trend.ma_days`, `sleeve_a.dca.chunk_pct_nav`,
`execution.rebalance_band`) and none of them is a stop, model, budget or schedule key.

| # | what | where | config only? | measured reason |
|---|---|---|---|---|
| 1 | Give `decide`'s escalation hysteresis, so an abstain streak and a regime-recovery artefact stop forcing the top-tier model. At minimum: drop `two_abstains` from the set, and require a flag to be *new* rather than merely true. | `runs/router.py:303-310` (`two_abstains`), `:350` (`if flags and flags.any()`) | **no** — code | 11 of 12 runs escalated; $1.5781/run vs $0.4348; **$7.7267 of $17.7939 bought "hold, confidence 0.50"**; `two_abstains` sole cause on 5 runs ($5.99) |
| 2 | Raise `daily_review.max_turns` from 40 to at least 80 — in **both** places — and audit `review`'s 150. | `config/earn.yaml:570` **and** `config/models.yaml:409` (review: `config/earn.yaml:577`, `config/models.yaml:394`) | **yes** | 2 of 3 runs died at 41 and 57 turns against a cap of 40; `config/models.yaml:198-203` already states the principle |
| 3 | Stop journaling a successful review as failed: `_session_outputs_ok` requires a `decision_grades` row that nothing has ever written. | `runs/daily_review.py:175-182` | **no** — code | 3 runs journaled `failed` for $10.7067 while writing 3 real reports + 3 grading packs (mtimes equal `finished_utc` to the second) |
| 4 | Generate `validate`'s citation allowlist from the evidence pack instead of maintaining it by hand. | `runs/signals/screener.py:118-149` (`verify`), validator call site | **no** — code | $8.6240 and **18 killed signals** per 8.21 days; 15 of 18 rejections "unknown feature_key"; live as of 2026-09-30T20:00:21Z |
| 5 | Schedule `python -m runs.signals resolve`, then backfill. **First** bound `_close_at`'s staleness, or a data gap writes silent 0.00% "neutral" grades into the table the replay scores against. | `config/earn.yaml` `ops.schedules` + `python -m ops.gen_ops_files` (no hand-editing `ops/crontab`); `runs/signals/outcomes.py:67-71` | **yes** for the cron line; **no** for the staleness bound | `resolve_due` is reached only from `runs/signals/__main__.py:90`; **20 of 31** stored validations resolve immediately under the shipped resolver, 19 of them `uncertain`/`invalid` — exactly the rows that score the validator's refusals |
| 6 | Make the console's usage page read both ledgers (or write the SDK stages into `llm_calls`). | `console/services/llm_service.py:333` | **no** — code | the page shows ~40% of spend and none of `decide`, the most expensive task |
| 7 | Resolve the dead config: wire `run_panel` or delete both `panel:` blocks; delete `extract` and `adjudicate`; either use `xhigh` or drop it from the ladder. | `config/models.yaml:282-295`, `:343-366`, `:114-132`, `:368-382`; `runs/router.py:44` | **yes** (deleting) | `run_panel` has no non-test caller; 12 decide runs produced one journal row each, not three; `extract`/`adjudicate` have 0 calls; no task uses `xhigh` |
| 8 | Re-size the per-task monthly budgets so they sum to something at or below the total, and only then discuss `budget.mode`. | `config/models.yaml` task `monthly_budget_usd` rows, `:424-427` | **yes** | per-task caps sum to **$292** against a $150 total; measured $217.70/30d; flipping to `hard` today would block `scan` immediately at 150% of a $3 cap |
| 9 | Hygiene: unpin `haiku` to the canonical `claude-haiku-4-5`. | `config/models.yaml:93` | **yes** | the only dated snapshot in the matrix; it is still served (135 calls, latest 2026-10-01T18:35:22Z), so this is tidiness, not an outage |

**Explicitly not in the list, and why:** raising `providers.ollama.options.num_ctx` and
`capabilities.local_small.max_ctx` from 8192 to 16384/24576 was recommended by two earlier passes
and measured to keep granite 100% GPU-resident (2.9 / 3.5 / 4.2 GB, latency flat 4.9/5.4/5.4 s).
It is a fine thing to do for headroom, but **its measured saving today is about $0**: the 34
context skips stopped on their own after 09-29 and the 8 remaining cloud scan calls are gray-zone
escalations, not skips (§4.4 item 4). If it is done, `config/models.yaml:104-108` requires both
numbers move together, and the repo's 40-prompt granite benchmark must be re-run at the new window
first — **screening accuracy at 15.7k tokens has never been measured**, only GPU residency.

---

## 8. What must NOT be built, with the measured reason

| do not build | measured reason |
|---|---|
| **A wider universe to reach the movers** | 4.18% of 1,843 doublings were inside the mandate, and that barely rises with the size of the move (3.74% at +200%, 4.36% at +500%). The eligible set already produces a 2x in 10.4% of names per quarter. Training on the wide universe measures **−0.126 Sharpe, t −1.934, winning 3 of 9 folds**. The funnel removing them is the tier/eligibility layer, by design (`docs/design/growth-audit.md`). |
| **A "moonshot" sleeve, book or tranche** | Buying what actually predicts a 2x (top `vol60` decile) inside the eligible set: **Sharpe 0.032, CAGR −25.2%/yr, max DD −99.3%** against BTC's 0.705 / +27.5% / −76.6%. The far-below-ATH decile: 0.170, −14.4%/yr, −95.0%. |
| **A feature or model that ranks "likelihood of a 2x"** | The AUC for a 2x runs **opposite** to the forward-return IC on all five features tested. Optimising for doublings is optimising against return, measurably. |
| **Flipping `trading.defaults.stoploss.trailing.enabled` on the strength of this work** | `distance_pct` is a profit-**from-open** ratio (`strategies/mechanics.py:225-237` returns `max_profit − distance_pct`), so the shipped 0.03 means a give-back of **1.5% of the peak** on a doubled position — below the entire pre-registered 5-40% grid. And the whole give-back family measures **−2.02pp (t −0.53)** against the exit that actually ships. The boolean is real and the inventory finding stands; the evidence to flip it does not exist. |
| **Flipping `trading.defaults.stoploss.atr.enabled`** | The repo's ATR stop is `current_rate − mult·ATR` over the open (`strategies/mechanics.py:240-252`), floored by the fixed stop through `max(candidates)` at `:277` — **not** the 22-day-high chandelier that was measured. Enabling it at period 14 / mult 3.0 measures **+0.20pp** versus today's config (+2.50% vs +2.30%), not +10.6pp. |
| **A new LLM "mover spotter" role, skill or detector** | The licence already exists and is on (`signals.scanner.screen.allow_novel: true`, `config/earn.yaml:375`) and fired **0 times in 9.21 days**; `signals.source='detector'` on 218 of 218 rows. It cannot spot a price mover even in principle: `runs/signals/screener.py:140-147` drops any novel item without a corroborated news hash, and `:128-139` drops any item citing a feature key Python did not compute — so a novel item is confined to *news* about pairs already on the computed watchlist. And nothing is graded yet (0 of 31 validations resolved, 0 decision grades). |
| **A model role that picks the exit level** | It is one swept number, and the model-writable field is already inert: `proposal.plan.stop_pct` is clamped to [0.03, 0.15] (`config/earn.yaml:284`) and read by nothing but a test. Adding a model to a dead field adds nothing. |
| **`budget.mode: hard`, today** | The caps are mis-sized (sum $292 vs $150) and have never been exercised, so flipping the switch starts blocking `scan` at 150% of a $3 cap rather than enforcing a sensible bound. |
| **A faster decision cadence, or a buy-share / volume-attention feature** | Standing measured negatives: reaction delay is free out to 48 hours and faster cadence costs **−0.75 to −2.4pp/yr**; recent buy volume is 0.991 correlated with total volume and buy *share* fails every test in the tradeable set with its **sign inverted** — volume and attention predict the drop. |

---

## 9. Limits, and what is not established

**On the movers work.** (1) No liquidity model: the full-panel books assume an equal-weight basket
of the highest-volatility microcaps can be bought monthly at 15 bps/side. That is false, which is
why that book prints an absurd CAGR; a real impact model makes those numbers worse, so the negative
is safe in that direction. (2) `max_tick_bps: 50` cannot be computed from daily OHLCV, so the
4.18% eligible share is an **upper bound** — the true figure is lower, which strengthens the
finding. (3) Delisted names are marked to their last panel print and then held in cash, which is
generous; if Binance delistings are in practice unexitable at that price, every number is
optimistic. (4) The three precursor features clearing the break-even IC is a **fired falsifier**,
reported as such; the claim that it is not news rests on the judgement that they restate
`universe.satellite_eligibility`. If a reader disagrees, the honest alternative reading is "the
eligibility filter is confirmed out-of-sample", which is still a positive about existing code and
not a new edge. (5) Monthly rebalance only; weekly and event-driven entry were not tested, on the
standing negative about cadence.

**On the exit work.** (1) The post-double sample is 173 episodes from 85 symbols, concentrated in
2021 (56) and 2024 (49); another bull cycle would settle it and nothing available today will.
(2) Everything is **daily** bars and daily closes, while the sleeves trade 4h — no result here
transfers without a 4h re-run. (3) The medians favour stops in every arm (+11 to +31pp), and Earn's
mandate is explicitly smaller drawdowns over faster gains — but those medians were computed against
the *strawman* baseline. **The median comparison against the real shipped exit (−10% stop + MA200)
was never computed, by either pass. It is not established, and it is the one honest route by which
a give-back rule could still earn its place.** (4) At a 90-day horizon the MA200 flip actually beats
the control (+2.08%, 2nd of 43), because the MA often never flips inside 90 days — the strawman's
failure is specific to holding periods long enough for it to fire. (5) The eligibility vol window
used was 120-day annualised where the shipped rule uses the 60-day `ann_vol_short`
(`ops/config.py:504`), which leaves two extra satellites in the universe; either window gives the
same 4.18%, so it does not bite on §1, but it slightly loosens §2's sample.

**On the model audit.** (1) It rests on **one 8.21-day window** inside which the host suspended for
10 hours, so per-day rates are floors and monthly figures are extrapolations; `review` is n=1,
`daily_review` n=3, the `brief` downgrade n=2. (2) Dollars are **notional** under the Max
subscription (0 of 436 rows metered); if `auth.claude_mode` moves to `api_key`, the same behaviour
becomes real money and the $30 cap at `config/models.yaml:75` binds within days at the measured
rate. (3) Neither ledger is complete and total spend is a **lower bound**: the primary fable
attempts for `daily_review` are recorded nowhere (`runs/daily_review.py:446-459` writes only the
last attempt), and those are precisely the attempts that authored two of the three reports.
(4) Interactive Claude Code sessions on this repo spend the same subscription and write no
`llm_calls` row, so true rate-limit consumption exceeds what is measured. (5) **Not established:**
whether the two turn-capped Opus retries produced anything beyond the fable attempts' reports;
whether a successful `daily_review` would have produced `outcome_*` columns as well as process
grades; whether Ollama is reachable from WSL right now (no loopback probe was made in this pass, and
`provider_health.last_ok` was 4 h stale when one earlier pass looked); and n=12 decisions cannot
support any inferential claim about whether escalation is *worth* paying for — settling that needs
120+ runs or a deliberate `opus@max` vs `fable@max` A/B on the same snapshot, which
`review.replay` already has the machinery for.

**On the live record.** The runtime tree `~/earn-run` is gitignored and mutable and was read at a
single instant (2026-10-01) by one earlier pass, which found `profiles.active: fast-test` there
against the committed `null` at `config/earn.yaml:620`. Everything in §3 about SleeveFast and 1-hour
candles rests on that one read. If the owner has since set it back and regenerated, the live
trailing stop goes dark again and the exit record becomes even thinner than described.
