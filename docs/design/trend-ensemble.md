# Trend ensemble — the build, the reproduction, and what the book does today

Status: built 2026-09-29 against `docs/design/dip-strategy.md` §0.3, §0.4, §8, §9 item 1 and §10.
Nothing here is switched on by this document; the strategies read a file the orchestrator has to
start producing (§4), and the sleeves are in TEST.

This document does not re-argue the case. `dip-strategy.md` made it and priced it: the fifteen-member
trend ensemble on BTC/ETH is the first book in this project to beat holding BTC on return, Sharpe and
drawdown over the longest window the data allows, **and** it does not clear the deflated hurdle (Sharpe
1.08 against 2.07, §6.2), is 86 years from statistical separation from hold (§6.3), and gives up 84
points of CAGR in a clean bull (§0.4). *The drawdown result is the finding; the return is a bonus.*

---

## 1. Reproduction — the document's numbers beside ours

Recomputed on this host on 2026-09-29 with `evals/trend_ensemble_backtest.py` (new) from
`runs/features/trend.py` (new), on `~/earn-panels/panel_1d.parquet` (747 tickers, dead coins in) and
independently on the feather store the containers read (`~/earn-run/data/binance/`). Same conventions
as the study: 2017-08-17 → 2026-09-24, 15 bps per side on every weight change, cash 0%, every signal
lagged one full day, Sharpe = CAGR / annualised vol.

**Full panel, the §0.3 headline** (tolerance stated: 0.5pp on CAGR / vol / MaxDD, 0.03 on Sharpe,
0.02 on gross — pinned by `tests/test_features/test_trend_ensemble.py::test_the_documents_headline_reproduces_on_the_panel`,
which skips where the panel is absent):

| book | CAGR doc / ours | vol doc / ours | Sharpe doc / ours | MaxDD doc / ours | gross doc / ours |
|---|---|---|---|---|---|
| BTC buy-and-hold | 38.6 / **38.59** | 66.9 / **66.88** | 0.58 / **0.58** | −83.2 / **−83.19** | 1.00 / **1.00** |
| BTC trend ensemble | 39.5 / **39.52** | 37.5 / **37.55** | 1.05 / **1.05** | −44.5 / **−44.53** | 0.46 / **0.46** |
| BTC/ETH trend ensemble | 43.1 / **43.07** | 39.9 / **39.94** | 1.08 / **1.08** | −45.6 / **−45.62** | 0.45 / **0.45** |

Exact to the document's rounding, 3,326 days. One-way turnover **8.62× NAV/yr**, fee drag
**1.29%/yr** — §8.4's numbers to the decimal.

**Sub-windows, §0.4 (BTC/ETH ensemble, CAGR / MaxDD / Sharpe):**

| window | doc | ours |
|---|---|---|
| 2019-01 → 2026-09 | 49.3 / −45.6 / 1.19 | 49.33 / −45.62 / 1.19 |
| 2021-01 → 2026-09 | 33.9 / −40.3 / 0.89 | 33.86 / −40.35 / 0.89 |
| 2023-24 (clean bull) | 53.7 / −28.3 / 1.47 (hold 137.6 / −26.2 / 2.82) | 53.70 / −28.34 / 1.47 (hold 137.56 / −26.15 / 2.82) |
| 2025-26 (chop) | 11.0 / −26.0 / 0.46 | 11.01 / −25.98 / 0.46 |

**On the feather store** (ends 2026-09-22, two bars short of the doc window, so a 3,324-day window):
BTC/ETH ensemble 43.53 / 39.94 / 1.09 / −45.62 / 0.45. The +0.5pp on CAGR is the two missing bars
(BTC fell over 09-23 → 09-24); everything else is identical. The two data sources agree, so the
strategies' signal and the study's signal are the same numbers.

Reproduce it yourself:

```bash
~/earn-dev/.venv/bin/python -m evals.trend_ensemble_backtest --panel ~/earn-panels/panel_1d.parquet
~/earn-dev/.venv/bin/python -m evals.trend_ensemble_backtest --data-root ~/earn-run/data
```

---

## 2. The members, verbatim from the study

`~/dp3/inc.py: ens_pos` (dip-strategy.md's provenance appendix); growth-audit.md §2.1 names the same
family. Equal weight, averaged into **one** book traded once — not fifteen books.

| # | member | rule | on / off |
|---|---|---|---|
| 1–9 | `ma50 ma75 ma100 ma125 ma150 ma175 ma200 ma225 ma250` | close vs own N-day SMA | 1 when close > SMA, else 0 (0 while the SMA warms up — the study's convention and the §0.3 "handicap") |
| 10–15 | `dc20_10 dc40_20 dc55_20 dc80_40 dc100_50 dc120_60` | Donchian state machine | on when close ≥ prior H-day high; off when close ≤ prior L-day low; else unchanged; 0 until the first event. "Prior" = the window ends at the previous bar |

The weight is the plain mean, so it lives on the grid 0, 1/15, …, 1. **Nothing is fitted and nothing
is selected** (§0.3) — the ensemble exists precisely so the MA integer never has to be chosen (§9
item 1: a 44-point CAGR range across one integer of lookback). Its weakness is named in §10.2 item 7:
the fifteen are worth about 1.23 independent bets; if trend following stops working, all fail together.

---

## 3. What was built, and where the ensemble is allowed to act

| piece | path | tier | what it does |
|---|---|---|---|
| The ensemble | `runs/features/trend.py` | 2 | `member_signals`, `ensemble_weight`, `applied_weight` (the one-bar lag), `asset_trend` (fails loudly on warm-up or a gap), `daily_closes` (feather ∪ knowledge DB), `compute_state`, **`write_state(cfg, kdb, root)`**, and the §10.1 checks `check_exposure_tracks_signal`, `check_fee_drag`, `expected_core_exposure` |
| The costed backtest | `evals/trend_ensemble_backtest.py` | 2 | §0.3 arithmetic, `DOC_HEADLINE` beside the measurement, a CLI |
| The in-container reader | `strategies/trend_state.py` | 2 | **stdlib only**, never raises; five named refusals (`trend_state_missing`, `trend_state_stale`, `trend_state_warmup`, `trend_state_no_asset`, `weight_zero`) |
| The gate and the scale | `strategies/earn_base.py` | 2 | `_trend_weight` / `_trend_scaled`, applied in exactly two places: `custom_stake_amount` (a new entry) and `_gated_add` (every add). `STRATEGY_VERSION` → `earn-3` |
| Tests | `tests/test_features/test_trend_ensemble.py`, `tests/test_features/test_trend_state.py`, `tests/strategies/test_trend_gate.py` | 2 | see §5 |

### 3.1 The state file: `knowledge/state/trend.json`

Written by `runs.features.trend.write_state(cfg, kdb, root)` next to `latest.json` and
`freshness.json` — the container already mounts `knowledge/` and already reads the freshness sidecar
from that directory, so no new mount. The knowledge DB carries only `ingest.cold_start_days` of daily
bars (13 today, the exact failure growth-audit.md recorded: *8 daily candles per pair against the 201
required*), and the feather store carries years but is refreshed on its own schedule (last written
2026-09-23 on this host). Neither alone can compute a 250-day average on today's bar. `daily_closes`
unions them, the DB winning on the overlap, drops any bar that has not closed, and the weight is
computed on the **last closed daily bar**.

```json
{
  "version": 1, "computed_utc": "2026-09-29T05:20:00Z", "timeframe": "1d",
  "members": ["ma50", "...", "dc120_60"], "warmup_bars": 250, "lag_bars": 1,
  "assets": {
    "BTC": {"weight": 1.0, "status": "ok", "asof_open_utc": "2026-09-28T00:00:00Z",
            "asof_close_utc": "2026-09-29T00:00:00Z", "close": 83500.01, "bars": 3330,
            "members_on": 15, "members": {"ma50": 1, "...": 1}, "detail": "feather+kdb"},
    "ETH": {"...": "..."}
  }
}
```

`status` is `ok` | `warmup` | `gap` | `no_data`; only `ok` carries a weight. **An empty signal is not
a flat signal** (§10.2 item 6) and it is not a full one either.

### 3.2 Where the ensemble acts — and where it is forbidden to

* **Entry gate.** For a `universe.core` asset (tier `core` in `riskgate.json`), `custom_stake_amount`
  multiplies the sleeve's desired stake by the weight; at weight 0 the stake is 0 and no order is
  raised. A satellite is never gated (weight 1.0, reason `not_core`).
* **Position scale.** Every add — scheduled DCA, rebalance top-up, pyramid/DCA add — goes through
  `_gated_add`, where the stake is clamped to `weight × target × NAV − position`, so repeated adds
  stop at the *scaled* target instead of each being scaled and still summing to the full one.
* **Never an exit.** The weight is read on the buy side only. Stops, the take-profit ladder, ROI,
  the exit signal and SleeveB's proposal-driven wind-down are untouched, and a test asserts
  `custom_exit` is `None` at weight 0 with an open position.
* **Never the risk gate.** The scaled stake is an *input* to `RiskGate.cap_stake` → `check_entry`,
  which still run on every order. `strategies/riskgate.py` is not modified.
* **Fails closed, and says why.** A missing, unparseable, stale (bar closed > 48h ago, or
  `trading.trend_ensemble.max_age_hours` when the config carries it), warming-up or absent-asset
  signal shuts the gate with the plumbing reason named, journalled once per pair per state change
  (`gate_decisions.reason = trend_gate:<reason>`, callback `custom_stake_amount` / `adjust_trade_position`,
  intent `entry` / `adjust`, severity `reject`). The Gate page and a healthcheck can tell
  `trend_state_stale` (the job is down) from `weight_zero` (every member is off).

### 3.3 Where it sits relative to the shipped MA200 gate

SleeveA still raises its entry candidate and computes its book targets from the 200-day-MA regime with
hysteresis, because that same regime drives its **exit** signal and the satellite `risk_on` switch, and
the mandate for this build was that the ensemble never causes an exit. So a SleeveA core entry today
is the **intersection**: regime up *and* ensemble weight > 0, sized by the ensemble weight. That is more
conservative than the doc's book, which is the ensemble alone. Replacing the regime outright (entries
*and* exits, §9 item 1 in full) is a follow-up that changes exit behaviour and needs the owner's
decision; it is recorded in §6, not done here.

SleeveB's targets come from proposals; the ensemble scales Claude's core weights on the way in — *code
decides whether that is allowed*. SleeveFast, the profile the paper bots run today, inherits the same
two chokepoints, so BTC/ETH entries on the 1h profile are gated too; its 29 satellite pairs are not.

---

## 4. Wiring — what the orchestrator has to do

1. **Call `write_state` after the candle phase of ingest.** Proposed: in `runs/ingest.py: Ingest.run`,
   a phase `("trend", self.write_trend_state)` placed right after `("candles", self.refresh_candles)`,
   where `write_trend_state` is
   `lambda: trend.write_state(self.cfg, self.kdb, self.root)` wrapped like the other phases (isolated,
   recorded in `ingest_runs`, never fatal — it is not in `TRADING_PHASES`). `runs/ingest.py` is
   another agent's file this run, so the call is proposed, not made. The scanner is an acceptable
   alternative host: anywhere a fresh `knowledge/earn.db` is open every 15 minutes.
2. **Run it once at deploy, before the strategies are reloaded**, or the gate is shut on both core
   assets for a plumbing reason until the first cycle writes the file:
   `cd ~/earn-run && .venv/bin/python -c "import sqlite3; from pathlib import Path; from ops.config import load_config; from runs.features import trend; k=sqlite3.connect('knowledge/earn.db'); k.row_factory=sqlite3.Row; print(trend.write_state(load_config(), k, Path('.')))"`.
3. **Optional config key** (tier 2, `config/**`, not touched here): `trading.trend_ensemble.max_age_hours`
   to move the 48-hour staleness line; the default is the module's and a test pins it.
4. **The healthcheck** can read the same file and alarm on `status != ok` or a stale
   `asof_close_utc` — §10.2 item 6 made that a stop trigger.

---

## 5. Acceptance — each item and the test that fails without it

| acceptance | test |
|---|---|
| reproduction table, doc beside ours | `test_trend_ensemble.py::test_the_documents_headline_reproduces_on_the_panel` (slow; skips without the panel); §1 above |
| rising-then-falling synthetic → 1 then 0, one-bar lag | `test_trend_ensemble.py::test_rising_then_falling_gives_weight_one_then_zero_with_a_one_bar_lag` |
| no look-ahead | `test_trend_ensemble.py::test_no_look_ahead_perturbing_the_future_leaves_the_past_untouched`, `::test_donchian_compares_against_the_prior_window_not_its_own_bar` |
| strategy refuses a core entry at weight 0, scales at 0.5 | `test_trend_gate.py::TestEntryGateAndScale::test_no_core_entry_at_weight_zero`, `::test_the_stake_is_scaled_by_the_weight` (SleeveA), `TestAddsExitsJournal::test_sleeve_b_is_gated_the_same_way` (SleeveB) |
| §10.1 check 1, exposure tracks signal ±0.10 | `runs.features.trend.check_exposure_tracks_signal`; `test_trend_ensemble.py::test_exposure_check_passes_inside_the_band_and_names_the_breach_outside` |
| §10.1 check 2, fee drag ≤ 0.15%/30d | `runs.features.trend.check_fee_drag`; `test_trend_ensemble.py::test_fee_drag_check_uses_the_documents_alarm_line` |
| the state job is idempotent, refuses warm-up, unions feather ∪ DB | `test_trend_state.py` |
| never an exit; never the risk gate; fails closed on missing/stale | `test_trend_gate.py::TestAddsExitsJournal::test_weight_zero_never_causes_an_exit`, `TestEntryGateAndScale::test_the_risk_gate_still_sizes_the_scaled_stake`, `TestMissingStaleWarmup::*` |
| the reader is stdlib only | `test_trend_gate.py::TestReader::test_the_reader_is_stdlib_only` |

---

## 6. The book today, and the honest limits

**As of the last closed daily bar (2026-09-28, closed 2026-09-29T00:00Z), computed from
`~/earn-run/data/binance` ∪ `~/earn-run/knowledge/earn.db` read-only:**

| asset | close | weight | members on | last change |
|---|---|---|---|---|
| BTC | 83,500.01 | **1.000** | 15 / 15 | 0.733 on 09-19/09-20 (the four shortest Donchians were off), 1.0 from 09-21 |
| ETH | 2,688.71 | **1.000** | 15 / 15 | 1.0 throughout the last ten bars |

So the book would be **fully invested in both core assets today**, as §8.5 said on 09-24. With the
current `base_weights` (BTC 0.40, ETH 0.30) the expected core gross for check 1 is 0.70 — before
SleeveA's own vol targeting, which is a separate reduction the check must be computed net of.

Limits, in the order they matter:

1. **Not an edge claim.** Everything in §8.6 of dip-strategy.md stands. Thirty days can only tell us
   the plumbing works (§10).
2. **The intersection in SleeveA** (§3.3) is more conservative than the measured book. Its return will
   differ from the reproduction; the reproduction is of the *signal*, not of the sleeve.
3. **Exits are asymmetric by mandate.** The measured book reduces exposure as members switch off;
   this build cannot, so a held position rides the shipped stop/ladder/regime exit instead. The
   drawdown behaviour of the live sleeve is therefore *not* the −45.6% of the study — it is whatever
   the exit stack produces, and the §10.1 exposure check will show the gap as a positive deviation
   when members switch off while positions stay on.
4. **The feather store on this host is refreshed on a slower schedule** (last 2026-09-23) than
   ingest. The union with the DB covers that as long as the DB's window (13 bars) overlaps the
   feather's end; if the feather ever falls more than `cold_start_days` behind, `asset_trend` reports
   `gap` and the gate shuts — loudly, by design.
5. **`write_state` is not wired**; §4 says where. Until it is, deploying the strategies alone shuts
   core entries on both sleeves with reason `trend_state_missing`.
6. **Fee model.** 15 bps per side, no impact, no intrabar path — §7 of the doc; every number moves
   down once real execution applies (§10.2 item 5).
7. **Trial count.** This build ran no new selection; the reproduction is the study's own
   configuration. §9 item 8 (advance `knowledge/state/trial_counter.json` by the study's ≈7,443 trials)
   is a human-only write and remains outstanding.
