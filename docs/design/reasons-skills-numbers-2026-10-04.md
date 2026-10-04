# Reasons to buy, reasons to sell, skills, numbers — 2026-10-04

Four audits, each then attacked by an independent reader who re-derived the numbers from the
same read-only snapshot (`~/pnlsnap/db`, taken 2026-10-03T23:3xZ), the live configs under
`~/earn-run`, `~/earn-panels/panel_1d.parquet` and a tokenless GET sweep of the console.
Where the attacker corrected a figure, the correction is in the row. Nothing refuted is
quoted bare.

---

## 1. Are we doing well

**No. But not for the reason the loss suggests, and not for the reason the dashboard suggests
either.**

The honest summary in four sentences:

1. **The system that is running is not the system that is designed.** Both bots have been
   executing `SleeveFast` from the `fast-test` profile since 2026-09-24T01:05Z — ten days.
   That profile's own file says, verbatim: *"It is a PLUMBING TEST, not an edge test"*
   (`config/profiles/fast-test.yaml:13`), *"net -1.29%"* over 30 days with costs on (`:126`),
   *"net -26.45%, MaxDD 26.8%"* on the 2024→2026 replay (`:151`), and *"this profile must not
   be left running"* (`:139`). It is also an **uncommitted local override** — `git status` in
   `~/earn-run` shows `M config/earn.yaml`, `M config/freqtrade-{a,b}.json`, `M
   config/riskgate.json`, `?? config/profiles/`. The live trading stack is not in version
   control.
2. **The money is close to flat and the fees are the story.** Current run realised
   **−13.14 USDT** (a −8.194146, b −4.943774). Gross **+24.89**, fees **38.02** — fees are
   **153% of gross**. The system earns about 25 USDT of trading and pays 38 to do it.
3. **Almost nothing is at work.** Mean capital deployed **3.38% of NAV** on sleeve a and
   **3.49%** on b; the book held **zero positions in 42.5% / 39.6%** of 454 NAV samples. The
   binding constraint is not the position cap (max 3 concurrent against a cap of 8) — it is
   `beta_cap`, which refused **6,864 of 10,913** entry attempts, **6,862 of them while the
   book held nothing at all**.
4. **The entries are not picking better than random; the exits are the healthy part.** Against
   a random hour in the *same coin over the same ten days*, entries rank at the **34th–39th
   percentile** (null 0.500) at every one of four horizons. Meanwhile the take-profit ladder
   fired 26 rungs and the stops cut losses at about −1%: nothing was mis-ordered on any of the
   22 losing exits.

**The pot, reconciled to the cent.** Seed 20,000. Cumulative **19,914.70** — down **85.30
(−0.43%)**. That splits into the abandoned 23-Sep run (**−69.77**: a −42.2106, b −27.5610),
the current run (**−13.14**) and two open positions marked to market (**−2.39**). Every one of
42 closed trades was recomputed from the order rows and matched its own stored P&L with max
|diff| 1e-8.

**What we cannot say.** Sharpe, max drawdown and the BTC-hold comparison (standing reference
0.83) **cannot be computed from the shipped console**: `sleeve_runs` has zero rows, so every
Performance endpoint returns empty. `btc_price` is populated on all 908 NAV points, so the
comparison is computable from data already in hand — nothing computes it.

**Corrections to the standing ground truth, so it stops circulating:**

| Was said | Actually |
|---|---|
| 34 closed trades | **38** in the current run, 42 across all run DBs |
| `exit_signal` 0/18, −115.91 | **0/22, −129.3162** — four more closed 2026-10-03T22:00Z (−13.405625) |
| net ~+0.27, fees 99% of gross | **net −13.137920**, gross **+24.886850**, fees **38.024770 = 152.8%** |
| avg capital 203 / 232 USDT (2.0% / 2.3%) | **338.32 / 348.75 USDT (3.38% / 3.49%)** |
| 9 of 15 proposals abstained | **11 of 19** journal rows (18 files on disk, one row's file is gone) |
| a 96.4h gap pushed SleeveB onto SleeveA's rules | **False.** Zero rows anywhere match `%drift%`. SleeveB was never loaded at all |
| dashboard flatters the pot by 40.15 / 26.27 | **Not established for the shipped UI.** `money.ts:138-139` prefers the pot; the gap is published as a named field `ledger_gap_usdt`. The genuinely flattering number is `run_pct` — see §5 |
| `ops_alerts` 2,880 rows | **3,275 rows**, all `delivered=0` (490 critical) |
| riskgate has 17 checks (`CLAUDE.md`) | **27** — `CHECK_ORDER`, `strategies/riskgate.py:63-69`, verified identical live and mirror |

---

## 2. Reasons to buy

Thirteen buy paths exist in the code. **One of them has produced 100% of the current run.**

| # | Buy reason | Correct? | Evidence (one line) |
|---|---|---|---|
| 1 | **`SleeveFast` `fast_breakout` — the only live path** | **WRONG (as production)** | The rule is honestly built — `SleeveFast.py:128-129` uses `rolling(lookback).max().shift(1)`, so no lookahead — but the profile that carries it says *"must not be left running"* (`fast-test.yaml:139`) and has run 10 days. All 40 current-run entries are `strategy='SleeveFast', enter_tag='fast_breakout'`, 2026-09-24T01:05:05 → 2026-10-03T23:00:04 |
| 2 | **Entry quality vs a random hour in the same coin** | **WRONG** | Percentile of each entry's forward return against every same-pair forward return in the window: H=1h **0.373** (n=21, p=0.383), H=4h **0.336** (n=21, p=0.078), H=8h **0.352** (n=20, p=0.041), H=24h **0.391** (n=18, p=0.238). Medians 0.234/0.275/0.291/0.301. Replicated to three decimals by the attacker. **Conclusion: no evidence of entry skill, negative point estimate at all four horizons** — not "proven negative edge" at n=21 |
| 3 | **Entry quality vs a coin flip (MFE/MAE)** | **WRONG** | MFE > \|MAE\| on **17 of 38** = 44.7%, worse than a coin flip. Mean MFE +1.414% / MAE −0.896%, carried by one trade (AVAX +10.50%). **10 of 38** never had a moment at which a 0.30% round trip was profitable. `exit_signal` subset: MFE +0.466% / MAE −1.288% |
| 4 | **`beta_cap` — what actually keeps 97% of the book in cash** | **WRONG** | `portfolio_beta` divides by the sum of *position values*; cash is not in the denominator, and `_book_after` starts from the held book. On an empty book the "portfolio beta" is the single candidate's own beta. **6,864 of 10,913** rejections (62.90%) are `beta_cap`; **6,862 carry `gross_exposure = 0.0`**. 16 of 31 whitelisted names measure 60d beta > 1.30 (PEPE 2.146 … WLD 1.304), and those are exactly the heaviest refusals (XLM 1440+719, AAVE 722+722, SUI 722+720, ZEC 721+720). `beta_cap` is check 24 of 27, so on every one of those occasions everything else had passed |
| 5 | Entry **order** is decided by beta bookkeeping | **WRONG** | AAVE (β 1.52) could only open 2026-10-01T23:36 while BTC (1.00) was held → (1.00+1.52)/2 = 1.26; XLM (1.35) only while SOL (1.22) was held → 1.285. Verified in both sleeves |
| 6 | **SleeveA 200-day regime rule, 2% hysteresis** | **OK (and dormant)** | `SleeveA.py:67-74`, `sleeve_common.py:38-48`; shipped `params-sleeve-a.json` `ma_days=200`, `hysteresis_pct=0.02`. Rule and numbers agree. It has produced **2 trades ever**, both 2026-09-23, both force-exited the same day. Note it is a *standing candidate*, not an event: every candle in an up regime sets `enter_long=1` |
| 7 | SleeveA's first entry is clamped to the DCA chunk | **WRONG, but the inference drawn from it was also wrong** | `SleeveA.py:70-72` tags every up-regime candle `dca`; `:203-204` clamps a `dca` entry to `chunk_pct_nav` 0.05. Real. **But** the claim that this forces "5% per week / six weeks to target" is refuted by the cited trades themselves: BTC took 3 chunks = 14.8% of NAV in 22 minutes on 2026-09-23 and ETH 2 chunks in 21 minutes — the clamp bounds each *order*, not each week. "The shipped sleeve would also sit in cash" is **not established** |
| 8 | **15-member trend ensemble as gate and scale** | **OK, and smaller than advertised** | Read at `custom_stake_amount` and `_gated_add`; scales the headroom; fails closed to 0.0 on missing/stale/warmup. **But it gates only `tier == 'core'`, and core is 2 assets (BTC, ETH)** of a 31-pair whitelist. Journal: 27 `trend_gate:not_core` vs 4 `trend_gate:ok`, **zero refusals**. `trend.json` is healthy (BTC 15/15 weight 1.0, ETH 14/15 with `dc20_10` off, detail `feather+kdb`, rewritten every 15 min) |
| 9 | **SleeveB — proposal / abstain / hold / drift** | **WRONG — never loaded** | `~/earn-run/config/freqtrade-b.json:83` says `"strategy": "SleeveFast"`. `consumed_status` NULL on all 19 proposal rows; all 117 orders carry `proposal_run_id` NULL; last `targets:*` gate row 2026-09-23T21:29:11Z (`hold_last_targets`); **zero rows anywhere match `%drift%`**. The drift path has never executed once outside a unit test |
| 10 | **Satellite cross-sectional ranker** | **WRONG — never run** | The ranker is real and well-built (`sleeve_common.py:180-260`, liquidity 0.40 / trend_quality 0.35 / long_trend 0.25, `hysteresis_ranks=3`) and the data is present (`universe.snapshot` carries **105** scores — tiers and caps number 107 — dated 2026-09-23, ZEC 0.996154, NEAR 0.963724, UNI 0.949344). It is reached only from `SleeveA._satellites`, and SleeveA is not loaded. **Correction:** readers of those scores do exist in five places (`SleeveA.py:120`, `gen_freqtrade_config.py:339`, `make_change.py:98`, two `evals/research` modules, two tests) — the defensible claim is *no running process reads them*. Under the live profile selection is first-come: 13 of 31 pairs ever traded, **18 never** (ADA, BCH, DOGE, DOT, ENA, FET, FIL, HBAR, PENGU, PEPE, PUMP, SUI, TAO, TRUMP, UNI, XPL, XRP, ZEC) |
| 11 | "Two satellites" is unreachable | **WRONG** | `max_satellite_positions=2` but `max_satellite_gross=0.05` while every target is 0.05 of NAV — a second satellite has no room. One `satellite_gross` refusal already journalled (XLM, sleeve b, 2026-09-30T19:53) |
| 12 | **Scheduled DCA / avg-down DCA / pyramid** | **OK** | Shipped: `dca.enabled=false`, `pyramid.enabled=false`, `scheduled_dca.enabled=true` (interval 7d, chunk 0.05). Firing: avg-down **0**, pyramid **0**, scheduled_dca **8 gate rows** all 2026-09-23T13:37–13:59 (BTC ×7 + ETH ×1), `SleeveFast` top-ups **0**. **Correction:** those 8 rows became **3 BTC fills + 2 ETH fills** (494.77+494.76+494.15 = 1483.68; 494.81+494.52 = 989.33), not "9 and 3 orders". Not one `adjust_trade_position` BUY row exists after 2026-09-23 — 40 trades, 40 single entries |
| 13 | **Can a scanner signal cause a buy?** | **WRONG — not today** | The pipeline is complete to the proposal (`runs/signals/pipeline.py:320-394`), but the last link is `SleeveB`, which is not loaded. 333 signals; 7 `acted` + 1 `planned`, all `news_event` with pair NULL; 144 expired; 74 screened out. The 7 acted signals produced proposals and **none was ever consumed**. **Correction:** the validator's `unknown feature_key` failures are larger than first reported — **17 of 48** rows, **12** distinct keys (not 10 rows / 3 keys): `market_state.data_fresh`, `.newest_data_age_min`, `.portfolio.modules.trend_signal`, `.portfolio.regime`, `.asof_candle_utc`, `watchlist.share_above_200d`, `.median_ret_1d`, `portfolio.breadth_above_200d`, `limits.max_weight`, `active_flags.missed_run`, `active_flags.data_stale`, `signal.detector_detail.pct` |
| 14 | **A/B is a benchmark** | **WRONG** | Both sleeves run the same strategy. **Correction to the first count:** pairing entries in order gives **17 matched pairings = 34 of 40 entries identical pair within ~5s**, and **three** differing pairings (#5 a=INJ/b=SOL 09-25T12:00, #10 a=SOL/b=BTC 09-30T13:41, #20 a=LINK/b=LTC 10-03T23:00) — not "38 of 40 / two differences". 85% duplication, and both differences that mattered were beta-cap accidents |
| 15 | **The gate itself** | **OK** | 27 checks; of 10,953 `confirm_trade_entry` rows **zero** were allowed with any check false. Flag expiry is handled correctly, so the stale `missed_run` flag is harmless. Check-key sets evolved honestly (1 row ×17, 2,316 ×26, 8,636 ×27) |

**Verdict on reasons to buy: DEFECTIVE.** Not one documented reason to buy is running. The one
that is running was never claimed to earn, measures worse than a random hour in the same coin,
and is gated by a cap whose arithmetic contradicts its own comment.

---

## 3. Reasons to sell

The sell *logic* is the healthiest part of the system. The sell *stack that is deployed* is not
the one in the docs, and carries three real defects.

| # | Sell reason | Correct? | Evidence (one line) |
|---|---|---|---|
| 1 | **Fixed stop** | **WRONG as documented** | Shipped `fixed_pct = 0.06` clamped by `stoploss_per_trade 0.15` → **−6%, not −10%**; `initial_stop_loss_pct = -0.06` on all 40 current-run trades (−0.10 only on the four 23-Sep trades). The running strategy has **no MA200 at all**. The 2026-10-01 study ("the −10% stop fires before the MA200 flip in 85% of episodes") measured SleeveA at 4h with a 200-day MA and a −10% stop; none of those three things is running. In 38 closed trades the fixed stop fired **zero** times |
| 2 | **Trailing stop — armed?** | **OK** | All four native Freqtrade trailing keys are **absent** from both 93-line bot configs; the trailing stop is entirely `custom_stoploss` (shipped `enabled true, activate_profit_pct 0.015, distance_pct 0.008, only_offset_reached true`). It works: **9 of 38 exits, 9/9 profitable, +41.3286** |
| 3 | **Trailing give-back mixes GROSS peak with NET profit** | **WRONG** | `max_profit = max_rate/open_rate - 1.0` (gross) is subtracted against a `calc_profit_ratio` that is net of both fees. Measured on all nine trailing exits: give-back **0.583, 0.587, 0.594, 0.591, 0.593, 0.583, 0.587, 0.591, 0.593 %**, mean **0.5891%** against a configured **0.800%** — **26% tighter than the config says**, shortfall 0.211% ≈ the 0.200% round trip, on every trade. Second effect: `is_stop_loss_trailing` is true on **six** trades that never reached the 1.5% activation (a14, a19, b3, b12, b14, b19 — corrected up from four), so a −6% fixed-stop loss can be reported as `trailing_stop_loss`. Safety is unaffected: Freqtrade's `StoplossGuard` counts both labels and strategy protections are honoured in dry-run |
| 4 | **`distance_pct` is a profit-from-open ratio** | **OK** | `mechanics.py:225-237` returns `max_profit - distance`, both from-open. On a doubled position 0.03 means a 1.5% give-back of current price. Immaterial at today's 0.008; real if it is ever raised. Document which basis is intended first |
| 5 | **ATR stop** | **OK** | `enabled: false`. Contributes no candidate. Its `timeframe: '4h'` is stale against a 1h system and would need fixing before it is ever enabled |
| 6 | **Effective stop = max(candidates)** | **OK in structure, WRONG in level** | `combined_stop_from_open` returning `max()` of from-open ratios is correct and the docstring matches. **Correction — this cannot be called "unambiguously right":** the same gross/net defect shifts the *fixed* stop too. On the two open trades `initial_stop_loss/open_rate−1` = −5.9982% / −5.9980% but `stop_loss/open_rate−1` = −5.8070% / −5.7985% — a ~19bp tightening, the same fee round trip, applied by `custom_stoploss` on every trade (`stop_loss_pct` spans −0.0693 to −0.0053) |
| 7 | **ROI table** | **OK** | `{0: 0.020, 90: 0.015, 300: 0.009}`; JSON key order is harmless (the resolver casts to int and sorts). **7 of 38 exits, 7/7 profitable, +74.8496** |
| 8 | One ROI exit below the current level | **NOT ESTABLISHED** | `ft_b_run` trade 5 (SOL) exited `roi` at 726 min with `close_profit 0.708%` against a 0.900% level. **The title "fired below any configured level" overreaches:** the `roi_table` in force on 2026-09-26 is unrecoverable — `config/riskgate.json` was regenerated 2026-09-29 and is uncommitted, `config/profiles/` is untracked, and `config_audit` holds only four 2026-09-23 rows. What *is* established: gate row 3821 recorded bid 122.04 / ask 122.05 = `max_rate` exactly, so the trigger rate was the ask; net there is 0.749% and at the 122.00 fill 0.708%, while **gross** is 0.951% / 0.910% — a gross basis clears it. Six of seven roi exits are consistent with the net basis, so an older `roi_table` is the more parsimonious explanation than a basis bug. One trade, +3.54 USDT — a curiosity, not a leak |
| 9 | **Take-profit ladder** | **OK — the healthiest exit in the system** | Rungs `[{0.009, 0.30}, {0.015, 0.40}]`; journal shows **tp1 ×16 and tp2 ×10 = 26 rungs**; `stake_amount/max_stake_amount = 0.42` exactly as 0.70 × 0.60 predicts |
| 10 | **Exit-reason attribution is an artifact** | **WRONG (the headline split)** | `trades` records only the final close reason while `close_profit_abs` is the whole trade including every rung. Rungs by final reason: **roi 8, trailing 16, exit_signal 2**. So "+74.85 from roi" and "+41.33 from trailing" are substantially **the ladder's money wearing another exit's name**, and the mechanical-vs-signal split overstates both sides. Attribute per sell order; the journal already has the rows |
| 11 | **`exit_signal` — the 22 losing exits: was anything missed?** | **OK — nothing was missed** | 22 trades, **0 wins, −129.3162**. (1) 14 of 22 were net-positive at their peak, but **20 of 22 peaked under +0.75% net** (corrected from 17) and the best peak across all 22 was +1.191%. (2) Only a14/b14 ever reached the +0.9% first rung and **both fired it**; zero reached +1.5% or the trailing activation. (3) The stop would not have fired first: worst trough **−3.109% net** against −6%, median **−1.027%**. **The exits are correctly ordered and are cutting losses at ~1%. The defect is upstream** — 22 of 38 trades never produced a +0.9% excursion |
| 12 | **`target_zero` has no KILL guard** | **WRONG, and broader than first stated** | `target_zero` is a *risk* reason, so `check_discretionary_exit` waves it through with no checks — the most permissive sell in the system — and its authority is a file another process wrote. **Correction:** `grep kill_engaged ~/earn-run/strategies/` returns **nothing**. `kill_engaged()` exists only in the un-deployed mirror, so the stated contrast ("the running strategy is missing the guard the non-running one has") does not exist in production. In what is live, **neither sleeve guards `target_zero` and the deployed `custom_exit` has no kill check either**. Reachable: engage KILL, have the weekly resolver drop a held name, regenerate `riskgate.json` → 100% of the position sold with zero gate checks. Not yet triggered (`mode.kill.engaged` false, `ops/killdir` holds only `.gitkeep`, `target_zero` exactly 2 rows, sleeve b, 2026-09-23) |
| 13 | **The de-risk size exemption** | **WRONG — it does not exist live** | **Correction:** `derisk_exempt_pct` is absent from the live `riskgate.json` risk block, absent from the live `earn.yaml` risk section, and absent from the deployed `strategies/riskgate.py`; it matches exactly one line in `~/earn-run` (`ops/config.py:1128`, written 2026-10-01, two days *after* the config was generated). "Shipped at 0.10" was a schema default read from the mirror. The conclusion is therefore **stronger**, not weaker: the live gate exempts an exit **only** on `is_risk_exit(exit_reason)` and then applies `orders_per_day` / `turnover_day` / `fee_budget` with no size branch — and neither `exit_signal` nor `roi` is a risk reason. **Every exit this system has ever made was a discretionary order a churn counter could veto**, with no threshold to miss. Zero of 104 filled orders reached 1,000 USDT; the largest was 505.55 |
| 14 | **How close the churn gate came to vetoing a sell** | **OK, and tighter than stated** | Counters are live and run-namespaced; day and month anchors current. **Correction:** on 2026-09-25 sleeve a reached `turnover_day` **4505.34** against a 0.50 × NAV cap of ~5,000 — headroom **~495 USDT, less than one position**. The *next* ~500 order that day would have been refused, not "two more". `orders_day` 12 of 16. September fees were higher than the October figures usually quoted: **13.44 (a) / 17.02 (b)** vs 9.49 / 9.99 |
| 15 | **The "daily-loss flatten"** | **OK — but it is not a flatten** | Shipped `daily_loss_response: "hold"`: at −3% the book **locks entries for 24h and sells nothing**, exactly as `earn.yaml` says. Never fired. Stop calling it a flatten |
| 16 | **`daily_loss_response: "halve"` is a dead branch** | **WRONG** | `loop_tick` returns `reduce=True` / `reduce_fraction`, and `bot_loop_start` inspects only `monthly_unlock` and `flatten` — the reduce fields are produced and discarded. Selecting "halve" would lock entries and **sell nothing**, while the config, the console Risk page and the docstring all claim the book is trimmed by half; the evidence for that setting is strong (+13.05% CAGR vs −5.23%). **Correction:** "zero call sites anywhere in the repo" is false — `reduce_pending` has ~29 call sites across five test files, and `tests/test_riskgate/test_daily_loss_response.py:55` already carries the docstring *"`adjust_trade_position` reads `reduce_pending`"*. A test file asserts the gate half of a contract whose strategy half was never written, which is worse than an untested branch |
| 17 | **Monthly stop and KILL** | **OK** | Monthly −10% is a true flatten held by a dated flag and released at the Gulf month boundary; armed against `month_anchor_nav` ~10,004 / ~10,008; never fired. KILL semantics match the 2026-09-30 decision — stops, trailing, the daily lock, force-exit and the price-sized ladder keep working, the trim is suspended — with the `target_zero` exception above |
| 18 | Zero of 22,020 gate rows ever refused an exit | **OK** | Nothing that should have sold was blocked. The churn exposure in row 13 is latent, not realised |

**Verdict on reasons to sell: MIXED.** The ordering, the ladder and the stops are right and are
doing their job. Three defects sit on top: the trailing stop is 26% tighter than configured and
mislabels exits, `target_zero` can sell everything under KILL with no checks, and every exit is
exposed to a churn counter with no size exemption to catch it.

---

## 4. Skills — is every skill needed working?

Twenty skills. The code is largely healthy; **the wiring is not.** Seven skills have no
automated production caller, one crashes on every CLI invocation, four depend on a weekly
session that has run once and failed, and the nightly grading loop has been paid for five
times and has written nothing.

| Skill | Working? | Evidence (one line) |
|---|---|---|
| **post-mortem / `write_grades.py`** | **WRONG — never called** | `decision_grades` = **0 rows ever**, `root_cause_events` = **0**. Yet the model's grading file exists for all 5 nights (`reports/daily/packs/{09-24,09-28,09-29,10-01,10-02}-grading.json`). Replaying the script's own two acceptance checks: **10-01 and 10-02 are schema-valid and would each write 3 grades + 10 root causes**; 09-28 is valid with 0 grades; 09-24 and 09-29 fail on **exactly one thing — `grades/N/note` `maxLength: 300`** (`schemas/grading.json:33`, verified). The script works, the permission exists, the data is valid; the session simply never runs the last step. Cost: **$19.0581** of $23.9696 total failed-run spend, out of **$56.8949** all-time. **Correction:** the `_grades_ok()` / "nothing in `runs/` writes decision_grades" citations are **mirror** line numbers; the live tree has `_session_outputs_ok` at `:175` with the check at `:180-182` — the function that actually failed the five runs is not the one quoted |
| **post-mortem / `lessons_tool.py`** | **WRONG — crashes unconditionally** | Line 150 verbatim: `p_app.add_argument(f"--{f}" if f != "path" else f, required=(f != "path") or None)`. argparse rejects the mere presence of `required` on a positional. `--help`, `lint <file>`, `append` and bare invocation all exit 1 with `TypeError: 'required' is an invalid argument for positionals`. md5 identical in all three trees. 299/296 passing skill tests cannot see it because every test calls the functions directly. Consequence: the one lesson in `lessons.md` was written by direct file write, bypassing lint and id-monotonicity |
| **venue-guard** | **WRONG — unwired, and the shipped copy is worse than the mirror** | `knowledge/state/venue.json` is **absent**; no cron line; zero flags carry `source='venue-guard'` (all 423 are `healthcheck`). **Correction that matters:** the "code is good" verdict was read off the *mirror*. The **shipped** `venue_state.py` raises a single `symbol_halted` flag at **scope ALL**, while the mirror scopes one flag per halted symbol — i.e. **one delisted satellite would block BTC and ETH entries on both sleeves.** Until it is wired, "we are protected against a USDT depeg or a trading halt" is false |
| **vol-surface** | **WRONG — unwired and stale** | `knowledge/state/volsurface.json` mtime 2026-09-25T00:20Z — **9 days old**. Nothing in `runs/`, `ops/`, `console/`, `config/`, `prompts/`, `strategies/` references it. A nine-day-old `sigma_hat` should not be readable by anything that sizes a position |
| **leverage-state** | **WRONG — never run** | `knowledge/state/leverage.json` **absent**. Code is sound (all 4 deterministic cases pass offline). Its 0.571 "pass rate" is an artefact of skipped `ask:` cases counted as failures — and see the gate row below: that number is not a gate score at all |
| **event-blackout** | **WRONG — never run** | `knowledge/state/macro.json` **absent**. Self-tests are genuinely good (14 assertions, 47 FOMC dates parsed, 54 meetings to 2027-12-08). The "calendar has run out" alarm it exists to raise **can never fire** |
| *(the three above)* | **pre-existing, not new** | `docs/design/crisis-policy.md:706` already logs this as **G4** ("venue-guard and event-blackout are bound to nothing and invoked by nothing"). The undocumented defect is the other direction: `docs/design/crypto-research.md:672-675` ships a table asserting vol-surface, leverage-state, venue-guard and event-blackout **"load" at named stages** — that stale table is what needs fixing |
| **decide** | **WRONG — never loaded** | `research_run.py:246-253` passes `allowed_tools=READ_ONLY_TOOLS` (`["Read","Glob","Grep"]`) plus `extra_disallowed=["Write","Bash"]` and **no `skills=` argument**. The 19 successful decide stages ran on prompt text alone. The repo asserts a decision procedure the decision stage cannot see |
| **asset-dossier** | **WRONG — scheduled on a coincidence** | Refresh runs only `if gulf_now().day == 1`. `knowledge/assets/` holds only `.gitkeep` dated 2026-09-22 — **not one dossier has ever been produced** — because no `daily_review` ran on 2026-10-01T17:30Z. The one night that would have built them was itself a missed run. Both scripts start cleanly: scheduling, not code |
| **tca** | **WRONG — never completed** | Reachable only from the weekly review session, which has run **once** (`review-2026-W40`, 2026-09-29T04:55:08Z, **failed**, 87 turns, $4.8794). The hourly `tca_job` is separate code and does work (`tca_fill_costs` 117, `tca_rolling` 32, `tca_calibrations` 2) but all 7 recent `ops_runs` rows show `finished_at` NULL. Given that fees are 153% of gross, this is the skill that should be screaming |
| **risk-gate** | **WRONG — never rendered** | Same single point of failure. The deterministic gate in `strategies/riskgate.py` is alive (22,020 rows); it is the weekly human-facing **report** that has never been produced |
| **strategy-lab** | **WRONG — one bug gates four skills** | **9 of 10 discovery runs failed**, 8 with `BacktestError: no backtest archive produced under …/ft_userdata/research/…` (latest 2026-10-03T22:20:02Z). `change_events` 0, `change_log` 0, `backtest_runs` 0, `replay_runs` 0 — **no change has ever gone through the protocol** |
| **research-scout** | **Code OK, blocked** | Properly wired (`discovery.py:119`), healthy, and blocked on the same backtest failure. The clearest case of good work stopped by one upstream defect |
| **hypothesis-lab** | **Code OK, exercised once** | `llm_calls task='discover'` = **1**, 2026-09-26T00:08:02Z. The other nine runs died before reaching the model |
| **edge-audit** | **Partly alive** | `trial_counter.json` is fresh (2026-10-03T22:20Z) — the trial counter *is* being kept. **Correction:** `decay_panel.py` is not "invoked by nothing" — `evals/cases.yaml:27` declares it and five tests call it; the true claim is **no scheduled caller**, so the quarterly decay re-test that is the skill's headline promise never runs |
| **skill-smith** | **Healthy, never invoked** | Lints clean. Blocked on the weekly review. `config/skills-registry.auto.yaml` is still `skills: {}` / `bindings: {}` — no skill has ever been created through the gate |
| **market-state** | **OK** | Loaded by `importlib` and called before each decision; `latest.json` mtime 2026-10-03T12:00:04Z matches the 16:00 Gulf slot exactly; 23 `state_snapshots` rows. Two caveats: newest `asof_candle_utc` is 2026-10-02T00:00:00Z (a day behind the run), and its `validate` binding is dead (below) |
| **reg-watch** | **OK — the best-functioning model-facing skill** | **18 of 19** flags stages succeeded at ~$0.07 each. Zero flags carry `source='reg-watch'` and that is the **correct** outcome: it found nothing and took the no-change heartbeat path, visible as `updated_at` in `flags.json` |
| **crypto-brief** | **OK** | `briefs` 6 rows, newest 2026-10-03T04:32:14Z; `news_items` 856. Of 12 stages, 7 succeeded and 5 were deliberately throttled (a designed outcome). Blemishes: `register_brief.py` has no caller, and one briefs row carries an absolute path |
| **exchange-ops** | **OK by design** | A reference with no `scripts/`, not bound to any stage. "Nothing invokes it" is the intended state |
| **ops-runbook** | **OK by design** | Human-only (`disable-model-invocation: true`). Its 11 lint errors are all `tools.bash_wildcard` and all correct for a runbook that must restart containers and clear the kill switch — the lint has no concept of human-only skills, and those 11 errors mask real ones |
| **_template** | **OK by design** | 1 lint error = the unsubstituted `{{SKILL_NAME}}` placeholder. **Correction:** `tests/test_research/test_skills.py:120-142` copies the template, substitutes, and asserts `lint_skill(dst).ok` — and passes. So "a freshly scaffolded skill starts life lint-dirty" is **wrong** |

### Skill infrastructure

| Item | Working? | Evidence |
|---|---|---|
| **The skill-eval gate** | **WRONG — and misdiagnosed on the first pass** | The real gate is `apply_changes.py:376-378` → `verify_change.py:208-213` → `skill_eval.evaluate(trusted_root=root)`. `evals/skill_cases/` in `~/earn-run` contains **only `README.md`**, so all 20 skills return `total=0, cases=[]`, refused: *"no trusted eval fixtures for '<name>'; the skill's own evals/cases.yaml is tier 1 and cannot grade itself"* — **and that refusal fires before the sandbox check, so `security.agent_user` is never consulted.** The binding blocker is **20 missing tier-2 fixture files a human must author**, not `ops/setup_agent_user.sh`. **Consequence for every number in circulation:** 0.875 (research-scout), 0.833 (venue-guard, hypothesis-lab), 0.80 (edge-audit, vol-surface), 0.571 (leverage-state), 1.0 (skill-smith) all come from each skill's own `cases.yaml`, which the gate refuses by design. **They are not gate scores and must not be quoted as "at the 0.8 floor"** |
| **`skill_lint` coverage** | **WRONG, narrower than claimed** | **8 of 20 shipped skills fail** `evals.skill_lint`: `_template` 1, asset-dossier 1, crypto-brief 1, market-state 2, ops-runbook 11, post-mortem 1, risk-gate 2, tca 2. Dominant error is bare `Write` where the permitted set is `Write(knowledge/**)` / `Write(reports/**)`. `test_review_skill_lint` does **not** call `lint_skill`. **Correction:** three skills *are* lint-gated by real tests (research-scout and hypothesis-lab under `strict_tools=True`, skill-smith), so the accurate claim is **17 of 20 uncovered, including all 8 failing ones** |
| **The `validate` binding** | **WRONG — dead** | `earn.yaml` binds `validate: [market-state, asset-dossier]`, and `validator.py:311-313` passes them with `tools_profile="read_only"` — a profile whose `_WRITE_TOOLS` disallow list is `["Write","Bash","Skill"]`, pinned by `tests/test_llm/test_providers.py:92`. The stage is handed two skills and simultaneously forbidden the tool that invokes them and the Bash that would run their scripts. **50 validations have run this way** (48 `signal_validations` rows). `_skills_for` also bypasses `effective_bindings`, unlike `daily_review` and `review` |
| **Hourly snapshot job** | **Claim REFUTED; a different defect is real** | `~/earn-run/config/earn.yaml` has **no `snapshot:` key**, `~/earn-run/ops/crontab` has **no `cron-snapshot` line** (installed 17 jobs = live repo 17 jobs; the Windows mirror has 18), and **`runs/snapshot_job.py` does not exist in the live tree**. So nothing "went missing unnoticed", `gen_ops_files --check` passes correctly there, and installing the mirror's crontab would schedule a module that is not present. **The real defect is a stale deployment** — the live revision predates the feature |
| **Nothing can tell you any of this** | **WRONG** | `ops_alerts` **3,275 rows, every one `delivered=0`** (490 critical); `ops_incidents` 563. No delivery channel is configured. Every finding in this document was discoverable from the databases days ago |
| **Shipped vs mirror skill tests** | **Note** | 296 collected / 296 passed in `~/earn-run` and `~/earn-dev`; **299/299** in the Windows clone. The difference is `ops-runbook/SKILL.md` plus venue-guard's script and 3 tests — so the frequently quoted "299/299" describes the mirror, not what ships |
| **Deployed code is one version behind** | **WRONG** | Deployed `STRATEGY_VERSION = 'earn-3'`; mirror is `'earn-4'` (verified). Max `strategy_version` in the journal is `earn-3`. `earn-4` adds the trend trim *and* `_journal_sized_out`, so the running code **cannot write `action='size_zero'` rows** — there are none, and a whole class of refusal ("the strategy wanted an entry and sized it to zero before the gate got a turn") is invisible. `earn_base.py` differs by 143 lines and `riskgate.py` by 100 between live and mirror, both in the entry path; live `riskgate.py` has no `derisk_exempt_pct` at all and its satellite fallback defaults are 4 / 0.10 rather than the mirror's fail-closed 0 / 0.0 |
| **Feather candle store frozen** | **WRONG** | All 324 `data/binance/*.feather` files have mtime 2026-09-23T17:5xZ; `BTC_USDT-1h.feather`'s last bar is 2026-09-23T16:00Z. Live bots are unaffected (dry-run pulls public OHLCV), but **anything that backtests, replays or walk-forwards sees data ending the day this run started** — strategy-lab, hypothesis-lab, edge-audit's decay re-test, the decision replay. The trend writer survives only because it stitches both sources (`detail: 'feather+kdb'`) |

**Verdict on skills: MIXED.** The code is in better shape than the system. Seven skills have no
automated caller, one CLI is dead, four queue behind a weekly session that has never succeeded,
one backtest path blocks four more, the gate that is supposed to grade skill changes cannot
grade anything, and the alert trail that would have told you all of this delivers nothing.

---

## 5. Numbers — do they sum up, and are they accurate?

**The money arithmetic is impeccable.** Every independent re-derivation matched:

| Check | Result |
|---|---|
| All 42 closed trades vs their own prices and fees | **0 mismatches** at 0.01; max \|diff\| 1e-8 |
| `realised_all_runs_usdt` (API) vs DB sum | a **−50.40470269** = −50.40470269; b **−32.50474607** = −32.50474607 |
| `realised_current` + `realised_earlier` = `realised_all` | −13.13792049 + −69.77152827 = **−82.90944876** exactly |
| Sleeve sums → combined total, all 9 money fields | match to **<1e-6**; no double counting |
| `cumulative` = seed + realised + open mark | a 9948.34903536; b 9966.35365109 |
| `nav_points` identity | 10000 + −8.194146 + −2.05895844 = **9989.74689556** = stored |
| The 20,000 seed | two genuinely separate pots, each with its own `ft_userdata` tree, DBs and bot process |

**Where the numbers lie.** Ten surfaces disagree with each other or assert something they never
checked.

| # | Surface | Correct? | Evidence (one line) |
|---|---|---|---|
| 1 | **Trading page reconciliation badge** | **WRONG — structurally cannot fail** | `portfolio.py:51` reads `nav = balance["total"]`, then passes that same value in as *both* the ledger side (`:56`) and the exchange side (`:78`); `portfolio_service.py:105` computes `delta = exchange − ledger`. Live `delta_usdt: 0.0`, `mismatch: false`, `exchange.total == ledger.nav` bit-for-bit. The green "books agree with the exchange" is a tautology. **And the bigger hole the first pass missed:** the control that would actually catch a divergence is `ops/lib/reconcile.py:294` → `riskgate.py:949` check `reconcile`, scheduled `*/15` in both the repo and the installed crontab, building its exchange side from an independent balance snapshot — and it **has never run**: `reconciliations` 0 rows, 0 flags matching `reconcil` (all 423 are `missed_run`/`data_stale` from `healthcheck`), `reconcile` absent from the 8 jobs in `ops_runs`. Shipped `risk.reconcile = {block_on_mismatch: true, dust_usdt: 10.0, tolerance_pct: 0.005}`, confirmed by the wallet payload's `tolerance_usdt 49.96 = 0.005 × ledger_nav`. **The panel is cosmetic and the enforcing control is dark** |
| 2 | **`nav.cards[*].run_pct`** | **WRONG — sign flipped** | `overview_service.py:105-110` selects the first point `WHERE sleeve=? AND run_id IS ?`; all 908 `nav_points` rows have `run_id` NULL, so the filter matches the whole table and baselines on **9959.0256732 @ 2026-09-23T21:45:00Z** — the abandoned run — reading straight across the +42.21 restart step. It reported **+0.308 / +0.214** at the 23:36Z sweep and **+0.317/+0.224** and **+0.324/+0.213** at later sweeps, i.e. a gain on every sampling, while the true pot return is **−0.5165% (a) / −0.3365% (b)** and `pot.gain_usdt` is **−51.58 / −32.08**. `run_started_utc` presents the abandoned run's first tick as this run's start. Not rendered on Home today (`money.ts` prefers the pot) — **but any report, agent or future page reading `/api/overview` gets it** |
| 3 | **"ledger cash 10,000.00" tile** | **WRONG** | `portfolio.py:59` sets `ledger_cash = balance.get("starting_capital")` → `portfolio_service.py:112` → `WalletPanel.tsx:73` prints it as "ledger cash". Real cash is **9,492.56**, shown one tile away as "free USDT". As displayed, cash 10,000.00 + positions 498.15 = 10,498.15 against a stated NAV of 9,990.71 — a reader summing the card lands **507 high**. Same on sleeve b |
| 4 | **profit-gaps open mark + its provenance string** | **WRONG (defect real, magnitude corrected)** | `profit_gaps.py:630` calls `sleeve_pot(...)` with **no `marks=`**, so the candle branch is skipped and the **last closed daily bar** is used, while `:706-707` tells the reader the number came from *"the newest closed candle"*. Proven by inverting the reported unrealised: LINK 13.794 (1d, `close_time 1790985599999`) vs 14.114 (1h); LTC 69.890 vs 70.170. **Correction:** this is a **sawtooth, not a standing 13.30** — `trend.py:339-340` sets `cutoff = now − 1 day`, so the lag runs 0→24h across the UTC day. At 23:36Z the two cards showed 19,914.70 and 19,901.40 (13.30 apart); at 00:23Z, 23 minutes after the day rolled, both resolve to the same bar and agree exactly (open mark −1.17559269 / −0.28817712, cumulative 19,915.63 on both). **The code defect and the false provenance string stand; the fixed magnitude does not** |
| 5 | **`pot.fees_usdt` vs `pot.gross_usdt`** | **WRONG — two fee bases** | `pot_service.py:163-164` makes `gross = realised_net + fees_CLOSED` while `:167-168` makes `fees = fees_closed + fees_OPEN`. So −24.96905239 − 58.93953875 = −83.90859114 against a realised −82.90944876 — off by **0.99914238**, exactly the two open entry fees (0.499460 + 0.499683). Home therefore shows **58.94 on one card and 57.94 on another** for the same trades. Related: the per-sleeve `fees_usdt` the console publishes (19.5096 / 19.5143) **include** the open entry fee and sum to 39.0239, not the 38.02 closed-trade figure — a claim that the console excludes it was refuted |
| 6 | **"No NAV points yet"** | **WRONG — 908 exist** | `performance.py:94-95` resolves the run through `sleeve_runs`, which has **0 rows**, so the endpoint returns `points=[]` before ever touching `nav_points` — which holds **908 rows (454 per sleeve)** spanning 2026-09-23T21:45Z → 2026-10-03T23:30Z. The message sends the operator to debug `nav_tick`, which is working, instead of the missing run registration, which is the cause |
| 7 | **The benchmark explanation** | **WRONG** | `money.ts:319`: *"the BTC yardstick starts once the first day is recorded"* — ten days **are** recorded. `nav_points` has **zero `benchmark` rows** because `nav_tick` only sets `bench_source` when a `sleeve_runs` row exists. `btc_price` is populated on **all 908 rows**, so the comparison is fully computable today. The sentence describes a wait that will never end |
| 8 | **The whole Performance family** | **Empty, honestly labelled except where noted** | `/api/perf/summary` metrics null, `/api/perf/nav` 0 points, `/api/perf/attribution` `[]`, `/api/testruns` `[]`, `/api/testruns/test-a-000` and `/api/perf/runs/test-a-000` **404** while the bot demonstrably runs on `ft_userdata/a/runs/test-a-000.sqlite` with 20 trade rows and `pot_service` correctly names `current_run: "test-a-000"`. Root cause is upstream: `var/state/mode.json` is missing, so no run was opened through `ops.modes`. `/api/mode` reports this honestly (`verified: false, reason: "missing"`). `/api/perf/whatif` is empty for a **different** reason — nothing populates `whatif_nav` — so fixing `sleeve_runs` will not fix that tab |
| 9 | **Raw ledger NAV series carries the +42.21 restart step** | **WRONG, latent** | Sleeve a steps 9957.78944331 → **10000.0** across 23:30Z → 23:45Z on 2026-09-23: a database swap rendered as a gain. No component charts it today — the only live `.nav()` call site is `performance/index.tsx:98`, which returns empty — so it is a loaded gun, not a current lie |
| 10 | **`mode.sleeves` has two shapes** | **WRONG, minor** | `/api/mode` returns an **array**; `/api/overview` returns an **object** keyed `a`/`b`. `money.ts:101` does `Object.values(... ?? {})`, which happens to work on both by luck. `/api/nav`, `/api/risk/state` and `/api/sleeves` return **404** |
| 11 | `ledger_gap_usdt` as corroboration | **Note** | It is not an independent measurement: because `ledger_nav` is seed + current-run realised + open mark, `cumulative − ledger_nav` collapses to **exactly `realised_earlier_runs_usdt`** whenever the pot's open mark and the latest nav point's unrealised agree (verified: −42.21055669 / −27.56097158) |
| 12 | `nav_points` schema | **Note** | It stores **two** components, `realized_pnl` and `unrealized_pnl` — there is no seed column; the 10,000 comes from config. The arithmetic quoted above is unaffected |

**Verdict on numbers: MIXED.** The money reconciles everywhere it is summed. Six console
surfaces disagree with each other or assert checks they never performed, and the two worst are
a "books agree with the exchange" badge that compares a number with itself over a control that
has never run, and a `run_pct` that reports a gain on both sleeves while both are down.

---

## 6. Ranked: what is broken, worst first

**Tier 1 — MISLEADS.** Shows a wrong thing as right. This is the worst category.

1. **The live system is not the committed system.** `profiles.active: fast-test` is an
   uncommitted local override; both bots run `SleeveFast` at 1h; the committed config still
   says SleeveA/SleeveB at 4h with `fixed_pct 0.1`, `roi_table {'0': 10.0}`, empty ladder and
   trailing disabled. Every document, including `CLAUDE.md`, describes a different system. The
   profile's own file says *"must not be left running"*; it has run 10 days.
2. **The reconciliation badge over a dark control.** Green "books agree with the exchange" from
   `delta = x − x`, while the `*/15` `reconcile` job that actually sets a `block_entries` flag
   has never produced a single row. The one defect that would matter with real money.
3. **`run_pct` reports a gain while both sleeves are down** (+0.3% vs −0.5165% / −0.3365%),
   because `run_id IS NULL` matches all 908 rows and baselines on the abandoned run.
4. **`beta_cap` refuses entries on an empty book** — 6,864 refusals, 6,862 at
   `gross_exposure = 0.0` — because cash is excluded from the denominator, which contradicts
   the cap's own comment. Entry *order* is now decided by beta bookkeeping.
5. **The trailing stop is 26% tighter than configured** (0.589% give-back vs 0.800%) because a
   gross peak is subtracted from a net profit, and **six** trades are labelled
   `trailing_stop_loss` that never armed it — which corrupts the exit tally everything else
   rests on. The same ~19bp shift also moves the fixed stop.
6. **The exit-reason split is an artifact.** "+74.85 from roi / +41.33 from trailing" is
   substantially the ladder's money under another name (rungs by final reason: roi 8, trailing
   16, exit_signal 2).
7. **"ledger cash 10,000.00" is the starting seed.** A reader summing the Trading card lands
   507 USDT high; real cash (9,492.56) sits one tile away under a different name.
8. **"No NAV points yet" while 908 exist**, and a benchmark message promising a wait that will
   never end while `btc_price` sits on every row.
9. **Two fee totals and two pot totals on one screen** — 58.94 vs 57.94, and (for most of the
   UTC day) 19,914.70 vs 19,901.40 — with the open-mark number carrying a provenance string
   that asserts a freshness the code does not deliver.
10. **The skill-eval gate reports a standard it cannot apply.** The gate refuses all 20 skills
    for want of trusted fixtures, and the self-graded pass rates (0.875, 0.833, 0.80, 0.571)
    are circulating as if they were gate scores.
11. **`daily_loss_response: "halve"` is inert** while the config, the console Risk page, the
    docstring and a test's own docstring all claim the book gets trimmed by half.
12. **A full close has no size exemption and `target_zero` has no KILL guard** anywhere in the
    deployed tree, while the docstrings describe both protections.
13. **`CLAUDE.md` misdescribes the system it governs**: "17 checks" (there are 27) and
    "BTC/USDT and ETH/USDT only" (the live whitelist is 31 pairs).
14. **Every study run since 2026-09-24 off the feather store covers none of the live period** —
    the store ends 2026-09-23T16:00Z — and `volsurface.json` is 9 days stale.

**Tier 2 — BROKEN AND HONEST.** Does nothing, and does not claim otherwise.

15. **The nightly grading loop is paid for and thrown away.** 5 runs, all failed, **$19.06**;
    `decision_grades` 0 rows ever; the model's output valid on disk for all 5 nights, 2 of them
    ready to write 3 grades + 10 root causes each, 2 rejected only on a 300-character note.
16. **Nothing can tell you any of this.** 3,275 `ops_alerts`, every one `delivered=0`;
    563 incidents; no channel configured.
17. **One backtest-archive bug gates four skills** — strategy-lab, research-scout,
    hypothesis-lab, edge-audit — for 9 consecutive nights. `change_events` 0, `backtest_runs`
    0, `replay_runs` 0: no change has ever gone through the protocol.
18. **The weekly review is a single point of failure for four more skills** (tca, risk-gate,
    skill-smith, strategy-lab) and has run once and failed, 87 turns, $4.88.
19. **`sleeve_runs` is empty**, so Performance, Test lab, the benchmark and every metric built
    on them are dark, and `var/state/mode.json` being missing is the root cause.
20. **The deployed code is one version behind** (`earn-3` vs `earn-4`), so `size_zero` refusals
    cannot be journalled and the Gate page is blind to sizing refusals.
21. **`lessons_tool.py`'s CLI crashes on every invocation**, so the one lesson on disk bypassed
    lint and id-monotonicity.
22. **The `validate` binding is dead** (50 validations ran with two skills bound and the `Skill`
    tool disallowed), the **`decide` skill is never loaded**, and the validator asks for **12
    feature keys the resolver does not know** (17 of 48 rows).
23. **Three state writers have never run** (venue-guard, leverage-state, event-blackout) and
    **no dossier has ever been produced**, because the dossier refresh is gated on Gulf
    day-of-month 1 and that night was a missed run. The shipped venue-guard would also raise
    `symbol_halted` at **scope ALL**, blocking BTC and ETH on one delisted satellite.

---

## 7. Paths that exist and have never fired

Dead code in a trading system is a liability, not a feature. Each of these is untested in
production and must not be trusted with real money on the strength of a unit test.

| Path | Fires |
|---|---|
| `SleeveFast fast_breakout` | **40 entries — 100% of the current run** |
| SleeveA regime (200d) entry | 2 trades, both 2026-09-23, both force-exited same day |
| SleeveA calendar DCA | 8 gate rows, all 2026-09-23T13:37–13:59 → 5 fills |
| SleeveB proposal entry | 2 trades, both 2026-09-23 |
| SleeveB rebalance add | 2 rows, 2026-09-23 |
| SleeveB hold | 1 gate row, no trade |
| **SleeveA satellite rotation** | **0** |
| **SleeveB drift-to-A** | **0** |
| **avg-down DCA** (disabled) | **0** |
| **pyramid add** (disabled) | **0** |
| **`SleeveFast` top-up** | **0** |
| **trend-ensemble refusal** | **0** (evaluated 31 times, refused nothing; gates 2 of 31 names) |
| **signal → proposal → buy** | **0** |
| **fixed stop** | **0** of 38 closed trades |
| **ATR stop** (disabled) | **0** |
| **daily-loss lock** | **0** |
| **monthly flatten** | **0** |
| **`reduce_pending` / "halve" trim** | **0 — and no caller exists to make it fire** |
| **`reconcile` job** | **0 rows ever** |
| **`decision_grades` writer** | **0 rows ever** |
| **`whatif_nav` writer** | **does not exist** |
| **venue / macro / leverage state writers** | **never run** |
| **asset dossiers** | **never produced** |
| **decay re-test (`decay_panel.py`)** | **no scheduled caller** |

Eight of thirteen buy paths, and five of the sell/risk paths, have fired exactly zero times.

---

## 8. Honest limits

1. **Sample size.** The decisive entry test rests on **23 distinct (pair, entry-hour) events**,
   21 measurable at 4h. The direction is unanimous across four horizons and the attacker
   reproduced every figure, but only H=8h clears p<0.05 two-sided. This establishes **"no
   evidence of entry skill"**, not "proven negative edge".
2. **Snapshot only.** All trade and journal figures come from `~/pnlsnap/db` taken
   2026-10-03T23:3xZ. The live `earn.db`, `journal.db` and the `ft_userdata` sqlite files were
   never opened. Console figures are tokenless GET sweeps at ~23:36Z and ~00:23Z, so API and DB
   are minutes apart — that accounts for small open-mark drift and for the two different
   `run_pct` and `ledger_gap` values quoted, not for any gap reported here.
3. **Two trees.** `~/earn-run` (live, rev 65cd4d7) is **behind** the Windows mirror (70df853).
   Code behaviour was read from whichever tree is named in each row; **config values are always
   the live generated `riskgate.json` / `freqtrade-{a,b}.json`**, never a schema default. Three
   first-pass claims failed exactly on this and are corrected above (the de-risk exemption, the
   `kill_engaged` comparison, the snapshot cron job). `earn_base.py` differs by 143 lines and
   `riskgate.py` by 100 lines between the trees, both inside the entry path, so any mirror line
   number in an older write-up is offset.
4. **Betas.** The 60-day betas come from `panel_1d.parquet`, whose last daily bar is
   2026-09-24, so the window is 2026-07-27..2026-09-24 rather than each refusal's as-of date.
   The conclusion (16 of 31 names over 1.30; 6,862 refusals at zero exposure) does not depend
   on that precision.
5. **MFE/MAE** comes from Freqtrade's own `max_rate` / `min_rate`, which update at loop
   granularity and not while a resting order is open, so intra-bar and during-exit extremes may
   be understated. This cannot change the conclusion that 20 of 22 `exit_signal` trades peaked
   below +0.75% net.
6. **Fees** are the dry-run **0.001/side** actually stamped on all 40 trades (0.200% round
   trip), not the 0.30% costed assumption in the standing rules. The 0.30% includes ~5bps/side
   of modelled slippage a paper fill does not pay, so **live costs would be worse than
   everything measured here**.
7. **Sharpe, max drawdown and the BTC-hold comparison (0.83) were not computed** — every
   console endpoint that would carry them is empty, and recomputing them from `nav_points` was
   out of scope for this pass.
8. **One mechanism could not be established**: the sub-threshold `roi` exit on 2026-09-26. The
   `roi_table` in force that day is unrecoverable because the config was regenerated three days
   later and is uncommitted. The net-vs-gross arithmetic is reported; the cause is not asserted.
9. **UI behaviour** was established by reading the shipped TSX in the tree the console process
   runs from, not by rendering pages in a browser. "Not rendered" claims rest on grep finding no
   consuming component.
10. **"Nothing invokes it"** throughout means nothing under `runs/`, `ops/`, `console/`,
    `config/`, `prompts/`, `strategies/` on a scheduled or automated path. Three such claims
    were corrected above. Note `console/services/skills_service.py:647-650` can load **any**
    skill by name in a throwaway worktree from the Skills page, and a human slash command is
    always possible — what the seven unwired skills lack is an **automated production caller**.
11. **Nothing was changed.** No file in the repo or `~/earn-run` was modified, no service,
    container, cron or unit was touched, no POST/PUT/DELETE was issued, no `.env` was read, and
    every snapshot DB was opened `mode=ro`. The only file created is this one.
12. **Two findings are pre-existing, not new**: venue-guard and event-blackout being bound to
    nothing is logged as **G4** in `docs/design/crisis-policy.md:706`. The genuinely
    undocumented defect is the opposite — `docs/design/crypto-research.md:672-675` ships a table
    claiming four unwired skills "load" at named stages.
