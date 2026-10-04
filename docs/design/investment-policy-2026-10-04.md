# Earn investment policy — 2026-10-04

**No, we do not have an investment strategy today.** What is running is a profile whose own
file calls itself "a PLUMBING TEST, not an edge test" and says "this profile must not be left
running" (`config/profiles/fast-test.yaml:1`, `:13`, `:139`), and it is running the *whole*
book from an uncommitted runtime override, at a mean of 3.4% of NAV deployed, with fees at
153% of gross profit. One of thirteen buy paths produced 100% of the trades. Nothing about it
was chosen as an allocation; it is plumbing that was left on.

This document is the policy. It answers the four things that were asked, in order.

---

## 1. The answer, on one screen

**"One that makes money."** It makes money and it makes less money than holding Bitcoin.
Measured over 9.11 years with costs on: the recommended structure returns **31.9% a year**
as specified, or **17.8% a year as the code is actually deployed today**, against **Bitcoin
buy-and-hold at 38.7%**. There is no version of this where the return number beats Bitcoin on
the full sample, and I am not going to dress that up. What it buys is drawdown: **−17.9% worst
case deployed, −33.5% as specified, against Bitcoin's −83.2%.**

**"Do best decisions."** The best decision available right now is not an allocation, it is a
measurement. `decision_grades` has zero rows after five paid nights, `sleeve_runs` is empty,
908 `nav_points` carry zero `run_id`s, and 0 of 31 signal validations are resolved. Nothing
in this project has ever been graded, so no decision quality has ever been observed. Step 5
below fixes that, and until it is fixed every judgement about everything else here is
unreadable.

**"It shouldn't be all money used by one logic."** Your instinct is right and the honest
answer is uncomfortable: **measured, there is one logic and a cash weight.** The correlation
between the recommended book and the raw trend ensemble alone is **+0.997**. The second
bucket is the only genuine second bet (correlation to the core +0.518) and 46% of its
holding-days are a single coin. So what this policy splits is not *edge* — there is only one
of those — it is **capital use**: at most about 45% of NAV is ever exposed to the one logic
and typically 19%, and the other 66–81% is deliberately not. That cash weight, not a second
strategy, is what turns −83% into −18%.

**"A dynamic one."** Yes, and it is slow and price-only. Exposure moves on 22.9% of days, in
increments of about 1.6 of 15 members, with a measured half-life of 43–60 days. No term in
this policy reads the book's own P&L, and that is not a style preference: the control that
does the chasing on purpose was measured and it **loses on all three axes** (−4.80pp CAGR,
−0.187 Sharpe, 2.70pp worse drawdown).

**Nothing here clears the statistical bar.** The best arithmetic Sharpe measured anywhere in
this exercise is 1.262, against a deflated hurdle of ~2.98 at ~8,500 cumulative trials. It
does not clear the repo's own lower figures either (2.323 from
`ml.metrics.deflated_sharpe_hurdle`, 2.07 at `runs/features/trend.py` §6.2). This is the
best-evidenced structure available in this project. It is not a proven edge.

---

## 2. What you are actually buying

Full sample 2017-08-17 → 2026-09-23 (3,324 days, 9.11 years), costs always on at 15 bps per
side (0.30% per round trip), cash earning 0%, **arithmetic** Sharpe (mean/std × √365).

| Book | CAGR | Sharpe | Max DD | DD/CAGR | Turnover | Fees |
|---|---|---|---|---|---|---|
| **BTC buy-and-hold** (the baseline) | **38.69%** | **0.828** | **−83.19%** | 2.15 | 0.06 rt/yr | 0.02%/yr |
| 50/50 BTC/ETH, daily rebalanced | 37.45% | 0.808 | −87.92% | 2.35 | 0.06 rt/yr | 0.02%/yr |
| Trend ensemble alone, 100% gross | 42.38% | 1.095 | −45.50% | 1.07 | 4.34 rt/yr | 1.30%/yr |
| **THIS POLICY, as specified** | 31.90% | 1.124 | −33.54% | 1.05 | 2.93 rt/yr | 0.88%/yr |
| **THIS POLICY, as the code is deployed** | **17.75%** | **1.262** | **−17.88%** | **1.01** | 1.84 rt/yr | 0.55%/yr |
| 100% cash at 0% | 0.00% | — | 0.00% | — | 0 | 0 |

The two rows in bold are the same policy. The difference is a volatility scalar that the
deployed code applies and that none of the four design submissions modelled — §5 is about it,
and it is the single largest number in this entire exercise (−14.7pp of CAGR for +14.3pp of
drawdown).

**Lead with the deployed row.** It is what would actually happen. 20,000 USDT becomes roughly
90,000 under it over nine years, roughly 232,000 under the as-specified version, and roughly
393,000 just holding Bitcoin. Bitcoin makes more money. That is the trade.

### The risk-adjusted case, with its error bar attached

| Comparison | Δ Sharpe | 95% CI | p |
|---|---|---|---|
| This policy (as specified) − BTC hold | +0.297 | [−0.172, +0.756] | 0.224 |
| Trend ensemble alone − BTC hold | +0.267 | [−0.215, +0.736] | 0.283 |
| Second bucket's contribution | +0.019 | [−0.029, +0.067] | 0.459 |

**Every interval straddles zero, including the project's famous ensemble edge.** Nine years
of one asset class is not enough to resolve it. The *drawdown* result is different in kind: it
is a structural consequence of averaging 19–34% gross exposure instead of 100%, not an
estimated mean, which is why it is the part I am willing to stand behind.

### The one control that matters for a drawdown claim

If the product is drawdown reduction, the right question is whether simply holding less
Bitcoin gets it more cheaply. Matched at the policy's own 28.18% realised volatility, the
control is 42.1% BTC + 58% cash:

| | CAGR | Sharpe | Max DD |
|---|---|---|---|
| Vol-matched BTC + cash | 21.33% | 0.828 | −48.21% |
| This policy, as specified | 31.90% | 1.124 | −33.54% |

The machinery beats cash dilution by **+10.6pp of CAGR, +0.296 of Sharpe and 14.67pp of
drawdown**. That is the strongest single result in the file, and it is the one that says the
trend signal is doing real work rather than acting as an expensive way to hold less crypto.

### What it costs you in a bull market — read this before agreeing

Calendar years, this policy (as specified) against BTC hold:

| Year | Policy | BTC | Gap |
|---|---|---|---|
| 2017 | +32.0% | +211.8% | **−179.8pp** |
| 2018 | −14.9% | −71.6% | +56.7pp |
| 2019 | +43.8% | +89.6% | −45.8pp |
| 2020 | +121.9% | +307.3% | **−185.4pp** |
| 2021 | +90.5% | +62.7% | +27.8pp |
| 2022 | −17.2% | −65.2% | +47.9pp |
| 2023 | +45.9% | +165.9% | **−119.9pp** |
| 2024 | +38.8% | +114.1% | −75.3pp |
| 2025 | +2.0% | −6.1% | +8.1pp |
| 2026 | +6.6% | −5.0% | +11.6pp |

In a clean parabolic bull it captures well under half the move. It beats BTC in only **43.9%
of 365-day windows** and **45.9% of 730-day windows** — over two years it is a coin flip which
wins, except that one of them can lose you 65% and the other has never lost more than 17%.

**It will not feel better month to month.** It is negative in 51.2% of 30-day windows against
BTC's 45.7%. The benefit lives entirely in the deep tail: negative in 23.5% of 365-day windows
against 35.5%, and 5.9% of 730-day windows against 24.2%; worst 365-day outcome −29.7% against
−83.1%; worst 730-day outcome −17.4% against −64.9%. That last number is the one I would put
on the wall.

**The single-day tail, which no design stated.** At maximum gross, a −20% day costs **15.0% of
NAV** and the signal lags one full daily bar, so nothing can react. The worst day actually
measured was −11.00%.

**The honest one-line summary:** roughly a fifth of Bitcoin's drawdown for roughly half its
return, and the price is that in the next real bull market you will watch Bitcoin double while
you make a third of that, and you will want to abandon this policy at exactly the wrong moment.

---

## 3. The allocation

Percentages are of NAV. "At full signal" is before the de-risk stack in §5; "deployed mean" is
what was measured with that stack on.

| Bucket | Target | The rule | What it is for | Turnover |
|---|---|---|---|---|
| **1. Core trend** — BTC + ETH | 70% at full signal (BTC 40 + ETH 30); range 0–70%; **deployed mean 18.7%** | `target = base_weight × (1 − satellite_gross) × 1[own 200d MA up] × vol_scalar`, then every buy and every trim is clamped to `ensemble_weight × target × NAV`. `ensemble_weight` is the equal-weighted mean of 15 price-only members — close above its own SMA for N ∈ (50,75,…,250) plus six Donchian state machines (20/10, 40/20, 55/20, 80/40, 100/50, 120/60) on *prior* windows — on the 16-point grid 0, 1/15, … 1, lagged one full daily bar. No fitted threshold, nothing selected. | The project's one surviving edge. Everything that makes money here makes it from this bucket. | 1.84 rt/yr ≈ 0.55% of NAV/yr |
| **2. Ballast** — loser-avoidance, 2 names | **5% declared, 0% funded today** (2.5% each) | The 2 lowest 60-day realised-volatility names among point-in-time `universe.satellite_eligibility` passers (ann vol ≤ 1.00, listing age ≥ 1095d, median 90d quote volume ≥ $10M), BNB excluded absolutely, reformed **quarterly**, equal weight. | A **capped research and observability budget whose expected return is zero.** Not a diversifier, not a return source — see below. Total ruin is 1,000 USDT, inside the policy's own worst measured 730-day outcome. | ~0.10 rt/yr ≈ 0.02%/yr |
| **3. Cash (USDT)** — a position with a job | The residue. Floor 25%, ceiling 100%. Measured mean 66% as specified, **~81% deployed**; 100% cash on 9.3% of days | Whatever buckets 1 and 2 do not ask for. Nothing may borrow against it. | **This is the diversification.** It is where −83% becomes −18%. Its three jobs: absorb the core's signal going to zero without ever forcing a hold; fund the next entry inside one rebalance interval; and earn a rate — *only* after §7 item 7 is satisfied. | Zero |

**The gross arithmetic fits inside every shipped limit with no cap raised.** 70% core (at
`risk.max_weight {BTC: 0.40, ETH: 0.30}`, `config/earn.yaml:170`) scaled by `1 − 0.05` plus a
5% ballast is 71.5% at absolute maximum, inside `risk.max_gross_exposure: 0.80` and leaving a
28.5% floor against `risk.usdt_floor: 0.20` (`:178-179`). The ballast is exactly
`risk.max_satellite_positions: 2` and `risk.max_satellite_gross: 0.05` (`:230-231`).

### Why the second bucket is a budget and not a bet, stated as numbers

| Measurement | With TRX | Without TRX |
|---|---|---|
| Ballast sleeve standalone, CAGR | 32.03% | 8.73% |
| Ballast sleeve standalone, Sharpe | 0.767 | 0.443 |
| Its contribution to whole-book Sharpe | +0.035 | **−0.002** |

TRX is **46.2% of all holding-days** over the only measurable window (2020-11-18 onward — no
name passes the 1095-day listing test before that, so 3.3 of the 9.11 years have no ballast
measurement at all). Its whole-book attribution is **+1.36pp of CAGR, +0.019 of Sharpe
(p = 0.459) and exactly zero change in max drawdown** (−33.537% with and without).

The sort itself is real and enormous — the 2 *highest*-vol eligible names return **−31.80%
CAGR at Sharpe −0.135 and −95.67% drawdown** — so loser-avoidance is a genuine signal. It is
just not a *return* signal, and one coin's history is not a portfolio. Both facts at once.
Anyone who reports only the first is selling you something.

So: fund it at 5%, expect zero, and treat it as the price of keeping the satellite machinery,
the tier caps and the exit path observable — because a book that only ever holds BTC and ETH
exercises none of them and will have no idea they are broken. **But do not fund it until the
four preconditions in step 8 exist**, because today each leg is refused at open, and a policy
that reports three buckets while running two is the kind of thing that has gone wrong here
four times this week.

---

## 4. The dynamic rule

**Three things move, all functions of price only, plus one overlay that may only ever cut.**

**(1) The core's exposure — the main lever.** Signal: the 15-member ensemble weight per asset
(`runs/features/trend.py:116-120`), each member a comparison of the close against its own
50–250 day moving average or a Donchian prior-window high/low. **Half-life 43–60 days** (from
lag-20 autocorrelation 0.727 BTC / 0.744 ETH, and lag-1 0.9875/0.9884). **Bounds** 0 to 70% of
the envelope, 0 to about 45% of NAV after the de-risk stack. It changes on **22.9% of days**
and moves **0.107 of full exposure** when it changes — about 1.6 of 15 members; median run of
an unchanged weight is 2 days, 90th percentile 10. It is at zero on 23.5% of days and at one on
18.1%. **A slow lever that moves in small increments, not a switch.**

**(2) The ballast's composition — quarterly.** Signal: 60-day realised volatility of *price*,
ascending, among point-in-time eligible names. 19 reforms in 9.11 years. The input is not the
ballast's own return, not its P&L, not which name made money. A name that fell 40% quietly
stays; a name that doubled violently leaves. Monthly reform measured materially worse, which is
consistent with the standing finding that faster cadence costs 0.75–2.4pp/yr.

**(3) The cash balance — continuously and passively, as one minus the other two.** No signal.

**(4) De-risk only, never a buy.** The `vol_ann_20d` cross-sectional percentile (AUC
0.634–0.654 for 7-day −20% drawdown risk) and the shipped HAR+DVOL forecast (OOS R² 0.60 BTC /
0.54 ETH) may **shrink** exposure and may **never authorise** an entry. They belong in the
existing crisis/flag path (`risk.crisis.block_entries_hours`, `risk.daily_loss_response`), not
in sizing. Volatility is forecastable; direction is not, and no amount of the first buys any of
the second.

### Why this is not performance-chasing — the falsifiable version

**There is no term anywhere in this policy whose value depends on the book's own outcomes.**
Not as a discipline, as a structure: the core signal is price against its own history, the
ballast signal is realised volatility and listing age, cash is arithmetic. A sleeve that lost
money yesterday gets exactly the same target weights as one that made money, and the code has
no column with which to tell them apart.

**And the thing you are worried about was measured.** The control reallocates 90% of the risk
budget every month to whichever leg won the trailing twelve months:

| | CAGR | Sharpe | Max DD |
|---|---|---|---|
| Fixed 70/5 split (this policy) | 31.90% | 1.124 | −33.54% |
| P&L-chasing control | 27.10% | 0.937 | −36.24% |

Chasing lost on **all three axes** over 9.11 years. So refusing to reallocate on realised
results is not only the disciplined policy, it is the better one in the only test that could be
run — and on 38 trades and 10 days of live history it would be fitting roughly one independent
observation.

**Three consequences you should expect and must not override:** (a) it will add exposure into
a market that has already risen and cut into one that has already fallen — that feels wrong and
it is the mechanism; (b) it will never increase the ballast because the ballast did well;
(c) it will never cut the core because the core lost money, only because price crossed its own
averages.

**Deliberately not dynamic:** the 70/5/25 bucket split itself, `base_weights`, every cap, every
band, and the 10,000/10,000 split between SleeveA and SleeveB. Those move on evidence through
`changes/*.json` and `runs/apply_changes.py`, never on a month's returns.

### The sleeves

**SleeveA** runs this policy deterministically from `config/params-sleeve-a.json`. No model in
the path. **SleeveB** runs the identical bucket structure and identical caps, and Claude's
proposal may do exactly two things: pick which ≤ 2 eligible names fill the ballast, and
**reduce** any target below A's computed value. It may never raise a target, move the split, or
touch a cap. Cut-only is the only discretion this project has evidence for, it makes every A-vs-B
divergence attributable, and `proposal.max_age_hours` plus `sleeve_b.drift_to_a_after_h: 48`
already bind a stale proposal back to A. SleeveB's expected contribution is **zero**; it is
funded to buy the grading data the system has never had.

---

## 5. The one dial that answers "makes money" — and it is tier-1

This is the most useful thing I found, and no design submission modelled it.

**The deployed code multiplies three independent de-riskers, and two of them are the same
signal.** For a core asset, effective exposure is:

```
base_weight × (1 − satellite_gross)        config/earn.yaml:170, :231
  × 1[the asset's own 200d MA is up]        strategies/sleeve_common.py:38, :310-312
  × min(vol_target_annual / book_vol, 1)    strategies/sleeve_common.py:320
  × ensemble_weight(asset)                  strategies/earn_base.py:655-680
```

`ma200` **is one of the fifteen ensemble members** (`runs/features/trend.py:116` includes 200),
so the binary 200d gate is a single member of the average applied a second time as a hard
switch. And `book_vol` is estimated at ρ = 1, the deliberately pessimistic exposure-weighted
mean (`strategies/sleeve_common.py:263-277`). The product of all three has **never been
measured** by anyone.

**The scalar's arithmetic** (derived from the code above, not measured): at BTC/ETH 20-day
annualised realised volatility of 0.40 / 0.50 / 0.60 / 0.70, the scalar at the shipped target of
0.30 is **0.75 / 0.60 / 0.50 / 0.43**. At a target of 0.50 it is 1.00 / 1.00 / 0.833 / 0.714 —
i.e. effectively off in calm markets.

**The dial is `sleeve_a.vol.target_annual`, it is live at 0.30
(`config/params-sleeve-a.json`, declared `config/earn.yaml:304`), and it is TIER-1 bounded
`{min: 0.10, max: 0.50, max_step: 0.05}` at `config/earn.yaml:315`.** It moves through
`changes/*.json` + `runs/apply_changes.py`. Four steps take it from 0.30 to 0.50.

**It is priced, at two measured endpoints only:**

| Configuration | CAGR | Sharpe | Max DD |
|---|---|---|---|
| Scalar on, target 0.30 (shipped, deployed) | 17.75% | 1.262 | −17.88% |
| Scalar off / never binding | 31.90% | 1.124 | −33.54% |

**−14.7pp of CAGR buys +14.3pp of drawdown.** No interior point was measured. Note also that
the shipped docstring records vol-targeting the ensemble *for return* at −8.4pp of CAGR —
it survives only as a priced drawdown option, which is exactly what this table is.

**Recommendation: leave it at 0.30 and do not touch it for the first measured quarter.** Then
move one step at a time with the A/B reading the result. If what you want is more return, this
is your dial and it is already tier-1 — you do not need a new strategy, you need to turn this
knob deliberately and watch what happens. Turning it without working measurement is just
guessing with a bigger position.

---

## 6. The ordered steps

Items 1–3 are preconditions, not improvements. Nothing below them is measurable until they
land.

| # | What | Where (key or file) | Tier | Kind |
|---|---|---|---|---|
| 1 | **Deploy the `beta_cap` guard to the running container.** The repo has `if measured < 2 or den <= _EPS: return None`; the runtime does not. Its own docstring records the cost: **6,864 of 10,913 entries refused, 6,862 while the book held nothing at all.** A policy that is 66–81% cash by design is permanently in the state that triggers it. | `strategies/riskgate.py:546-585` → runtime `~/earn-run` | 2 | deploy existing code |
| 2 | **Deploy `risk.derisk_exempt_pct: 0.10`.** Present in the repo, absent from the deployed gate. The MA200 flip close arrives as `exit_signal`, which is **not** in `RISK_EXIT_REASONS` and matches no `RISK_EXIT_PREFIXES` — so a 10%-of-NAV size test is its *only* exemption from the monthly fee counter. Without it, the one sell this whole design depends on is refusable, which has already happened once. | `config/earn.yaml:217`; `strategies/mechanics.py:48-53`; `strategies/riskgate.py:1163-1166` | 2 | config-only + `ops.gen_freqtrade_config` + re-bless |
| 3 | **Stop the fast profile in the runtime.** The committed value is already correct. Set it to `null` in `~/earn-run`, regenerate, re-bless, confirm `trading.timeframe` is back to `"4h"`. A profile can only touch `trading.*`, `execution.*`, `sleeve_a.*`, `sleeve_b.*` and `sleeves.<s>.strategy` — so risk limits were never altered, but the sleeve and the cadence were. | `profiles.active` (`config/earn.yaml:624`); `config/profiles/fast-test.yaml` | 2 | config-only |
| 4 | **Confirm the core signal exists and is fresh in the runtime.** The entire core bucket reads one file and **fails closed to weight 0** on `trend_state_missing` / `_stale` past 48 hours. In this Windows mirror `knowledge/state/trend.json` is **absent** and `latest.json` reports `data_fresh: false` with `assets: {}`. If the runtime looks like that, the book sits in cash no matter what step 1 fixes. | `knowledge/state/trend.json` via `runs/features/trend.py:write_state`, read at `strategies/trend_state.py:44-49`, `:123` | 2 | verification |
| 5 | **Turn on the measurement.** `sleeve_runs` empty; `decision_grades` 0 rows after five paid nights (the model's output was valid on disk all five, two rejected on a 300-character note limit — the failure is in the **writer**, not the prompt or the budget); 908 `nav_points` with 0 `run_id`s; the `run_pct` query whose `run_id IS NULL` matches all 908 rows and therefore reports +0.3% while both sleeves are down; 0 of 31 signal validations resolved. | `ops/sql/**`, the grading writer, the `run_pct` query, `journal/**` | 2 | code + schema |
| 6 | **Do NOT rewrite the core sizing path — add a test instead.** The claim that "nothing in the order path reads `knowledge/state/trend.json`" is **false**: `_trend_scaled` is called at `:737` and `:1029`, the sell side at `:629-653`, the trim at `SleeveA.py:241-312`. Acting on that claim would reopen a buy/sell asymmetry already found and fixed on 2026-09-30. Assert instead that every non-`ok` reason is no-entry and that only `ok`/`weight_zero` may size a sell. | `strategies/earn_base.py:580-680`, `:623`; `strategies/SleeveA.py:241-312` | 2 | test-only |
| 7 | **Measure the deployed product of all three de-riskers** before changing any one of them, and publish the menu. The best number available (17.75% / −17.88%) includes the vol scalar but **not** the binary 200d gate, so it is still optimistic on gross. | the three sites listed in §5 | 2 (code) / 0 (the report) | measurement |
| 8 | **Ballast preconditions — all four, before the bucket is funded above zero.** (a) new `risk.max_noncore_gross: 0.05` checked over *book minus `universe.core`*, because `is_satellite` keys on `tier == "satellite"` and `satellite_gross` uses it, so a `major`-tier ballast name is bounded only by `tier_caps.major: 0.15` — three times the intended bucket; (b) a feasibility assertion that every leg clears `min_position_pct_nav` **after** the vol scalar: 2.5% × 0.60 = 1.5% is refused at OPEN, and 2.5% × 0.43 = 1.08% is at `dust_weight`; (c) a **low-vol ranker**, which does not exist — `select_satellites` ships and is LIVE but ranks on `universe.score` (liquidity 0.40 / trend_quality 0.35 / long_trend 0.25), which is explicitly not this rule; (d) the new key needs `description` / `x-tier` / `x-group` via `ops.config.F(...)` or `tests/test_foundation/test_config_schema.py` fails. | `strategies/riskgate.py:151-152`, `:1059-1062`, `:1065-1069`; `config/earn.yaml:176`, `:234`, `:300`, `:118-120`; `strategies/sleeve_common.py:224`; `strategies/SleeveA.py:120-133` | 2 | code + config |
| 9 | **Calibrate the cost assumption.** `fee_bps: 10.0`, `slippage_bps: 5.0`, `measured_month: null`, `n_fills: 0` — the block says it is rewritten monthly by `runs/tca_job.py` with measured medians, 42 closed trades exist, and the calibration has never run. Every fee number in every document here rests on a seed. | `config/backtest.yaml` costs block via `runs/tca_job.py` | 2 | run the job |
| 10 | **Hold the band and the fee budget exactly where they are.** `execution.rebalance_band` stays 0.05 and `risk.max_fee_pct_per_month` stays 0.01. Reasons in §7 items 4 and 5. | `config/earn.yaml:299`, `:235`, bounds `:318` | 1 (deliberate non-action) | config-only |
| 11 | **Last and smallest: the two arithmetic items.** BNB fee tier needs a carve-out making a reserve balance not a position, against `universe.data_only_symbols: ["BNB/USDT"]` which marks it NEVER tradeable; cash yield has **no key anywhere** (`yield`, `cash_apr`, `flexible` all return zero hits in `config/earn.yaml`) and is a claim on an exchange, not spot USDT. | `config/earn.yaml:82`; new `cash.*` keys | 2 | human decision first, then code |

---

## 7. What must NOT be switched on until something is measured

1. **Any allocator that reads realised results.** No reallocation on P&L, sleeve returns, win
   rate, trade count, fees paid or grades — not between buckets, not between sleeves. The
   system has no row to do it from, and the control that does it anyway measured worse on all
   three axes. This is pre-refused in writing because it is the next thing that will be asked
   for.
2. **A dynamic SleeveA/SleeveB capital split.** Stays 10,000 / 10,000 until `decision_grades`
   has rows and `sleeve_runs` is non-empty. On 38 trades that is one independent observation.
3. **The ballast bucket above 0%,** until all four preconditions in step 8 exist. And until
   then the policy must be *reported* as two buckets, not three.
4. **Tightening `risk.max_fee_pct_per_month`** (0.01 → anything). The intermediate scale-out
   trims this policy lives by are about 0.107 × target × NAV ≈ 3–5% of NAV — *under* the 10%
   de-risk exemption, classified `rebalance_trim`, and `ACTION_PRIORITY` ranks `add` above
   `rebalance`, so risk-*increasing* orders spend the budget the de-risk later needs, and the
   counter is keyed to the Gulf month so one refusal repeats until the month turns. Tightening
   it 4× makes drawdown protection refusable in exactly the expensive month. Add a **tripwire**
   instead: the repo already knows the number — `FEE_DRAG_ALARM_30D = 0.0015` at
   `runs/features/trend.py:136` — and the risk config simply does not read it.
5. **Stepping `execution.rebalance_band`.** Two agents measured the same frontier and reached
   opposite recommendations (one says tighten to 0.02 for +2.4pp of CAGR, the other says 0.05 is
   best on Sharpe with a cliff at 0.08), the 0.03 waypoint was never measured by anyone, and the
   surface is flat within noise from 0.00 to 0.05. Do not act until one run reports CAGR **and**
   fee drag **and** Sharpe on the deployed book including the interior point.
6. **Removing or loosening the vol scalar,** until step 7's measurement exists. Then it is a
   deliberate, journaled, tier-1 step of 0.05 — not a side effect of a strategy change.
7. **Booking cash yield in any number.** Nobody has verified that a flexible-yield product is
   available to this account on Binance UAE, nothing in the repo moves idle USDT, it sits
   awkwardly against a spot-only mandate, and "instant redemption" is a policy rather than a
   guarantee. Holding 66–81% of NAV as a claim on an exchange is a **larger left tail than
   anything the trend signal protects against** — a Celsius-shaped haircut on that balance is a
   −66% event that appears in no table in this document. If it is ever wired, quote the
   conservative simple-interest figure (+0.66pp of CAGR per 1% APR, not the +0.88pp geometric
   one) and never in a headline.
8. **Changing `trading.timeframe` from `"4h"`.** About 108 tests assert it, the stop, trailing
   stop, ROI and exit ladder all run on that loop, and the standing finding is that reaction
   delay is free out to 48 hours — so there is no measured basis and a real downside.
9. **Any buy-side use of `vol_ann_20d`, the HAR forecast or leverage state.** De-risk only,
   forever.
10. **Claude anywhere in the order path.** Not a policy choice; an invariant.

---

## 8. Honest limits, and the arithmetic against the hurdle

**The statistics.** Best arithmetic Sharpe measured anywhere here: **1.262**. Hurdles in
circulation: **~2.98** (brief, ~8,500 cumulative trials), **2.323**
(`ml.metrics.deflated_sharpe_hurdle(0.828, 8500, 9.11)`), **2.07**
(`runs/features/trend.py` §6.2, at 177 trials). **Nothing clears any of them** — the best result
is 42% of the strictest and 61% of the most lenient. The four design agents declared 27 + 13 +
20 + 12 = **72 trials**; the adjudication added roughly 20 more; **this document adds zero** (no
backtest was computed here). Cumulative goes from ~8,500 to ~8,592, which moves the deflated
hurdle by **less than 0.01** — so the hurdle is not what this fails on. It fails by a factor of
about 2.4, and more trials would not change that.

**The ten things that could make the numbers above wrong.**

1. **Trend following stops working.** The single point of failure, and the policy does not hide
   it. All 15 members average 0.802 pairwise correlation and are worth about **1.23 independent
   bets** — if trend breaks they break together. The kill case is a multi-year choppy range: the
   core whipsaws across its own averages, pays about 0.55–0.88% of NAV a year for nothing, and a
   5% ballast cannot rescue the book. 2017, 2019, 2023 and 2024 are the mild version.
2. **Costs are an uncalibrated seed** (`measured_month: null`, `n_fills: 0`, 42 closed trades
   available). The *ranking* survives a 3× slippage error; the absolute fee figures do not.
3. **The execution tail is unpriced.** All turnover comes from the target-weight path.
   `exit_timeout_count: 3` routes a failed limit exit to an emergency **market** order paying the
   spread, not 5 bps — and the core going to zero is exactly that sale. Separately,
   `scheduled_dca {enabled: true, interval_days: 7, chunk_pct_nav: 0.05}` builds a position in
   7 weekly chunks, so a 0 → full core build takes about seven weeks, which no backtest modelled.
4. **The de-risking sale may be refused.** See §7 item 4. Every drawdown figure here is an
   **upper bound on behaviour**, not a measurement of it.
5. **The deployed book is still not fully modelled.** 17.75% / −17.88% includes the vol scalar
   but not the binary 200d gate stacked on top of a signal that already contains `ma200`. Gross
   would fall further; the effect on CAGR is unmeasured.
6. **The ballast is one coin.** 46.2% TRX holding-days, 5.85 of 9.11 years measurable, an
   eligible pool averaging ~11.6 names — so "best of 300 random draws" is really closer to "best
   of about sixty distinct pairs". That is a much weaker statement than it reads as.
7. **The two measured ballast figures disagree between agents** (32.03% / 0.767 versus 11.1% /
   0.482 on overlapping windows) and I could not reconcile them: the scratch directories are in
   WSL and unreachable from this session. The direction check agrees in sign and magnitude; the
   level does not. Sized at 5% with an expectation of zero, the disagreement does not change the
   policy — which is the point of sizing it that way.
8. **One sample, one asset class, two or three independent cycles**, in which BTC rose 38.7% a
   year. If crypto's unconditional drift is gone, a book that is 66–81% cash and 19–34% long is a
   worse way to hold less crypto than simply holding less crypto.
9. **Two universe filters were never applied** in any measurement: `max_tick_bps` (tick size is
   not in the panel) and `min_weekend_volume_ratio`.
10. **Owner behaviour, the most likely failure of all.** This policy loses to BTC in 56.1% of
    365-day windows and 54.1% of 730-day windows, and in a real bull it will underperform by
    75–185pp in a single calendar year. There is no version of this where that feels acceptable
    while it is happening. **If it will be abandoned mid-drawdown-avoidance to chase a rally, it
    will deliver the worst of both and should not be started.** A pre-committed review rule
    instead of a monthly one: if over any rolling 12 months the policy trails BTC by more than
    15pp **and** its own realised drawdown never exceeded 20%, then the insurance was never
    collected and the premium was wasted — at that point raise the vol target one tier-1 step or
    hold more BTC outright, decided **once** on that rule and never monthly on P&L.

**Provenance, stated at the right confidence level.** Every CAGR, Sharpe, drawdown, turnover,
correlation, bootstrap, window, calendar-year and control figure in this document was measured
by other agents in this run on `~/earn-panels/panel_1d.parquet` (survivorship-free, costs on at
15 bps/side). **I could not audit any of it:** the WSL scratch directories and the four
pre-registration sha256 seals are unreachable from this Windows session, so every backtest
number here is a self-report at one remove. What I **did** verify, line by line against the
repo, is the code and config layer — the `measured < 2` guard, the vol scalar and its ρ = 1
estimator, the three-way de-risk stack, `ma200`'s membership in the ensemble, `is_satellite`
keying on tier, the `min_position` opening-only test, `exit_signal`'s absence from
`RISK_EXIT_REASONS`, `ACTION_PRIORITY`'s ordering, the tier-1 bounds on
`sleeve_a.vol.target_annual`, the empty take-profit ladder, the absence of any yield key, and
the absence of a low-vol ranker. **Two of those checks contradict the design documents
materially** and are flagged at steps 6 and 8. **Derived from code and not measured:** the vol
scalar's value at given book volatilities (§5), and the size of a typical scale-out trim
relative to the 10% de-risk exemption (§7 item 4) — both are open arithmetic on verified
constants, so both are checkable.

No repo file other than this one was modified, no git operation was run, no snapshot database
was opened, nothing was written under `~/earn-run`, and no network call was made.

---

## 9. The one thing to take away

You asked for something that makes money, does its best thinking, spreads the risk, and
adapts. What this project actually has, measured and after costs, is **one price-only trend
signal that is good at avoiding disasters and mediocre at making money, a large deliberate cash
position that does most of the risk reduction, and no working way to tell whether any of it is
behaving.** This policy is the honest arrangement of exactly that, and the first real decision
is not an allocation — it is steps 1 through 5, after which the numbers in this document stop
being somebody's backtest and start being your book's.
