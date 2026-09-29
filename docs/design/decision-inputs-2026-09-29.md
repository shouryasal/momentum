# Decision inputs: handing the decision stage its indicators (2026-09-29)

Fixes `docs/design/paper-trading-review-2026-09-29.md` §2 #7 and §6 item 4, and closes §7
check 5's second question ("does the newest proposal still cite `assets={}` /
`asof_candle_utc=null`?"). Owner of this change: `.claude/skills/market-state/scripts/compute_state.py`
(the writer of `knowledge/state/latest.json`) and `tests/test_research/test_compute_state.py`.

## 0. The answer

`knowledge/state/latest.json` now carries populated indicators for BTC and ETH, computed on the
last **closed** daily bar from the feather store ∪ the knowledge DB — the same
`runs.features.trend.daily_closes` series the trend ensemble reads, imported, not copied. On a
copy of `~/earn-run`'s data the file went from `assets: {}` / `asof_candle_utc: null` /
`regime: unknown` to two full asset blocks, `asof_candle_utc: 2026-09-28T00:00:00Z`,
`regime: trend_up`, `data_fresh: true`. It can no longer be silently empty: a missing core
asset puts one readable line per asset in `reason`, sets `status` to `partial` or `empty`,
and prints the same line to stderr so the research log shows it. The decision prompt
(`research.v4`, `{{STATE}}`) receives the file verbatim, so an abstain can now cite the cause
instead of `assets={}`.

## 1. The defect, measured

| what | before | after (copy of the runtime data, 2026-09-29 05:58Z) |
|---|---|---|
| daily bars available to `asset_state` | 12 closed (DB only; 13 rows, the 09-29 one open) | 3,330 (3,324 feather 2017-08-17 → 2026-09-22, + 6 DB bars 09-23 → 09-28) |
| bars needed | 201 (200d MA + one return) | 201 |
| `assets` | `{}` | `BTC`, `ETH` populated |
| `asof_candle_utc` | `null` | `2026-09-28T00:00:00Z` (bar closed `2026-09-29T00:00:00Z`) |
| `portfolio.regime` | `unknown` | `trend_up`, `vol_regime: med`, `trend_signal: long`, breadth 1.0 |
| `reason` | field did not exist | `null` (status `ok`) |
| proposals citing it | 7 of 7 abstained on empty inputs, 11.86 USD | — (the next run reads the populated file) |

The DB alone can never reach 201: `ingest.cold_start_days` keeps ~13 daily bars. The feather
store cannot alone either: it is refreshed on its own schedule (last written 09-23 on this
host, ends 09-22). The union is the series; the DB wins where both hold the same date (they
agree to the cent on 09-17 → 09-22).

## 2. What Sleeve B is finally told (copy of the runtime data, as of the last closed bar)

| | BTC | ETH |
|---|---|---|
| bar (open → close, UTC) | 2026-09-28 → 2026-09-29 00:00 | same |
| close | 83,500.01 | 2,688.71 |
| 50d MA | 76,501.25 | 2,404.22 |
| 200d MA | 71,173.97 | 2,104.93 |
| trend (1% hysteresis vs 200d MA) | up (+17.3% above) | up (+27.7% above) |
| realized vol, 20d, annualized | 0.4377 (med) | 0.4681 (med) |
| drawdown from 90d high | −3.60% | −3.15% |
| 1d return | −1.15% | 0.00% (2688.65 → 2688.71, real: the 1h series confirms it) |
| funding, 8h | 0.0059% | 0.0087% |
| source | feather+kdb | feather+kdb |

The file: 1,532 characters against `build_prompt.INPUT_BUDGETS["state"]` of 1000 tokens
(3,500 characters), so `_truncate` leaves it untouched.

## 3. What changed in the writer

1. **Data path.** `asset_state(kdb, pair, *, data_root, now)` calls
   `runs.features.trend.daily_closes(pair, kdb=, data_root=, now=)` and computes on what it
   returns. `data_root` is `$EARN_DATA_DIR` else `<root>/<paths.data_dir>`, the same rule as
   `trend._data_root` (a private helper, so mirrored in four lines rather than imported).
   `now` is the run's clock: bars whose close is after it are dropped by `daily_closes`, so
   the indicators are always on the last **closed** bar, never on the open one.
2. **Refusals are reasons.** `asset_state` returns `(indicators, None)` or `(None, reason)`;
   it never returns `(None, None)`. Three refusals, each one line naming the counts, the
   sources and which store to refresh:
   - no bars at all: `BTC: no closed daily bars: neither the feather store (binance/BTC_USDT-1d.feather under <root>/data) nor knowledge/earn.db candles(tf='1d') has any`
   - too few: `BTC: 12 closed daily bars (kdb, 2026-09-17 -> 2026-09-28) < 201 needed for the 200d MA; the feather store has no binance/BTC_USDT-1d.feather under <root>/data; refresh data/binance so history reaches back at least 201 days`
   - a hole between a stale feather and the DB's window: `BTC: 6 calendar day(s) missing (2026-09-10 -> 2026-09-17) inside the last 201 bars (feather+kdb); refresh data/binance so the feather store overlaps the DB's window` — a 200-bar average across a hole is not a 200d MA, so it is refused, exactly as `asset_trend` refuses a gap for `trend.json`.
   Reasons are ASCII only: `json.dumps` escapes anything else, and the prompt is the file
   verbatim.
3. **The file says so.** New top-level fields: `status` (`ok` | `partial` | `empty`),
   `reason` (null, or `assets block incomplete - the decision stage has no indicators for: <one line per missing asset>`),
   `missing` (asset → its line), `inputs` (bars needed, feather store, DB), `watchlist`
   (below). Per asset: `asof_candle_close_utc`, `bars`, `source`, `ret_1d`. Existing fields
   and their semantics are unchanged: `asof_candle_utc` stays the bar's **open** time (the
   `state_snapshots` rows and the console's chart markers read it that way), `portfolio`,
   `data_fresh`, `newest_data_age_min` as before. `compute_and_write` returns the state and
   prints `compute_state: status=<s>: <reason>` to stderr whenever `reason` is set; `main()`
   exits 1 unless `status == ok`.
4. **Core blocks, watchlist counts.** Before, the loop ran over `cfg.universe.pairs` — the
   tradeable tier, 31 pairs on the runtime's 2026-09-23 snapshot. Had it ever had the data,
   31 asset blocks (~10k characters) would have hit the prompt's 1000-token state budget, and
   `_truncate` keeps the **tail**: BTC and ETH, early in sort order, would have been the first
   things cut out of the decision prompt. `assets` now holds `universe.core` only, and the
   other tradeable pairs are summarised in `watchlist` as counts — `n`, `computed`, `missing`,
   `above_200d`, `share_above_200d`, `median_ret_1d` — which is what SKILL.md asks for
   ("breadth across the watchlist, in one line … never a per-name story"). A test asserts a
   populated file with feathers for every tradeable pair stays under the budget.

`runs/research_run.py` is untouched: it already calls `cs.compute_and_write(cfg, kdb, root,
now)` before any model call and ignores the return value; the reason reaches the model through
the file and reaches the log through stderr.

## 4. Tests (`tests/test_research/test_compute_state.py`, 7 tests; all fail without the fix)

| test | fails without |
|---|---|
| `test_a_300_bar_feather_plus_a_13_bar_db_populates_both_core_assets` — temp root, 300-bar feather to 09-22, 13 DB rows (12 closed) to 09-29; both assets populated, `asof_candle_utc = 2026-09-28T00:00:00Z`, 306 bars, `source = feather+kdb`, `close` is the DB's, `ma200`/`ma50` equal the union's; audit row carries the stamp | the union (DB alone → `assets: {}`) |
| `test_the_db_alone_at_13_bars_is_refused_with_the_reason_the_review_measured` — the runtime's exact shape; `status = empty`, each `missing[...]` says `12 closed daily bars (kdb …) < 201 needed` and names the feather; stderr carries it | `reason` / `status` / stderr |
| `test_with_no_daily_data_at_all_the_file_says_why_and_the_decide_prompt_carries_it` — no feather, no DB rows; `reason` names both assets and both sources, is ASCII, is on disk, and appears verbatim in `build_prompt.gather_inputs(...)["state"]` and in the built `research.v4` prompt | `reason`, ASCII |
| `test_a_partial_block_names_the_missing_asset_and_keeps_the_other` | `partial` |
| `test_a_hole_between_a_stale_feather_and_the_db_is_a_refusal_not_a_200_bar_average` | the gap check |
| `test_a_populated_state_fits_the_prompt_budget_even_with_a_31_pair_universe` — feathers + DB rows for every `cfg.universe.pairs`; `assets` is core only, `watchlist.n = 29`, file ≤ 3,500 chars and `_truncate` is the identity | core-only + counts |
| `test_regime_changed_utc_carries_forward_across_reruns_on_the_union` | idempotence |

The pre-existing `.claude/skills/market-state/tests/test_state.py` (DB-only golden values, 291
bars) and `tests/test_research/test_skills.py::TestComputeState` pass unchanged;
`tests/test_research/test_research_run.py` (which copies the script into a temp root and runs
`main_flow`) passes unchanged.

## 5. What this does not do

- It does not refresh the feather store. If `data/binance/*-1d.feather` falls more than
  `ingest.cold_start_days` behind, the gap refusal fires and the file says so — the fix is to
  refresh the store, not to average across the hole. `trend.json` has the same dependency
  (`trend-ensemble.md` §"limits" item 4).
- It does not change `data_fresh`: that is still the book snapshot and the newest 1h candle.
  On the runtime copy at 05:58Z it was `true` (13 min); the 09-29 05:01Z rerun that wrote the
  stale abstain saw 5,320 min because the host had slept (`outage-2026-09-25.md`).
- It does not touch `runs/research_run.py`, `prompts/**`, `config/**`, `runs/features/trend.py`
  or SKILL.md. SKILL.md still says the script "reads knowledge/earn.db"; that sentence is now
  incomplete (it reads the feather store too) and is a tier-1 edit for a review session.
- It does not make Sleeve B trade. It gives the decision stage numbers; whether the next
  proposal is a `hold` or a `trend` allocation is the model's call inside the copied limits,
  and the risk gate's after that.

## 6. Deploy

Copy `.claude/skills/market-state/scripts/compute_state.py` and
`tests/test_research/test_compute_state.py` into `~/earn-run`. Nothing restarts: the research
run loads the script fresh from disk each run (`runs.research_run.load_compute_state`), no
bot, unit or the console reads it, and the web bundle is untouched. The first research run
after the copy — or `python3 .claude/skills/market-state/scripts/compute_state.py` by hand —
rewrites `knowledge/state/latest.json` populated. `runs/features/trend.py` must be present
(it is, since this morning's ensemble build).
