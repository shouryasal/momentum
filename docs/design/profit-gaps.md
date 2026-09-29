# The profit & gap ledger — 2026-09-29

Status: built, tested, first reading taken against a copy of the live data. Nothing here
changes what the bots do; it measures what they did and what stopped them.

The owner's ask, verbatim: *"keep checking what is likely profit we are getting and what are
gaps making us miss profit"*. The review of 2026-09-29
(`paper-trading-review-2026-09-29.md`) answered it once, by hand, for one week. This makes
that answer a job: `runs/profit_gaps.py` computes it, the daily review writes it every night,
`GET /api/profit-gaps` computes it on demand, and Home shows it.

---

## 0. What "likely profit" honestly means, and what can be measured instead

**Likely profit is a declared expectation, not a measurement.** The active profile has a
costed backtest of its own (`config/profiles/fast-test.yaml`, evidence block: −1.29% per 30
days, fees 1.35% of the balance, worst 30-day drawdown 1.46%, MaxDD 26.8% over 2024-2026);
the shipped 4h sleeves with the trend ensemble have a rolling-365-day distribution
(`dip-strategy.md` §8.2: median +24.8%, p25 +2.2%, p10 −15.0%, worst −38.9%; §8.3: plan for
−45%; §10: median 30-day −0.3%). Days of realised P&L are noise against those numbers —
`dip-strategy.md` §10 puts statistical separation from holding BTC at 86 years — so section
A of the ledger prints the expectation and, in the same breath, the sentence that days of
results cannot confirm or refute it. The card on Home does the same. It never prints a
realised number as a verdict on the strategy.

**What can be measured every day is the gap**: money the mechanism forwent or wasted for
reasons that are *not* the strategy — a host asleep, a flag that blocked entries, a validator
that errored on every signal, model spend that returned nothing, a watcher looking at the
wrong database, a ledger that reset to the seed, a hand-typed flatten. Each of those has a
size (USDT where it was realised, hours or a count where it was not), a query, and a cause.

---

## 1. The ledger, line by line

Two windows, always: **the last 24 hours** and **since the test began** (the first row in
any run database — `2026-09-23T13:37:36Z` on the live host). Every line is
`(label, value, unit, query)`; the query is the SQL or the file rule that produced the
number, so a reader can re-run it.

### A. Expected

`ops.config.load_config().profiles.active` names the profile. Its evidence block is parsed
from the profile file when present (`net X%`, `max drawdown X%`, `fees paid X% of starting
balance`, and the last `MaxDD X%` as the long-window envelope); otherwise a table in code
(`runs.profit_gaps.EXPECTATIONS`, keyed by profile, `None` = shipped) carries the numbers and
their source document. Shown as "expected per 30 days" and "planned max drawdown", scaled to
the window for reference, with the cannot-confirm-or-refute note.

### B. Realised

| line | source |
|---|---|
| cumulative pot, all run databases | `console.services.pot_service.sleeve_pot` — imported, not copied (built the same morning; seed + every closed trade in every run database + open positions marked to market) |
| realised in the window, gross, fees, fee/gross, trades, win rate, exit-reason mix | every `ft_userdata/<s>/{tradesv3,runs/*}.sqlite`, `mode=ro`; fees from the filled orders (`cost × fee` per side), because the trade row's `fee_close_cost` covers the last exit leg only |
| open positions, marked to market | the pot's `open_mark_usdt` |
| holding BTC / the equal-weight basket over the same window | `knowledge/earn.db` 1h candles at `since` and `until`, costed at 15 bps per side (`(1 − 0.0015)²`); the basket is every pair with 1h candles minus `universe.data_only_symbols` |

### C. The ten gaps

| # | gap | size | how it is measured |
|---|---|---|---|
| 1 | uptime | hours | `Bot heartbeat` lines in both `freqtrade.log`s merged; a gap > 10 min is dark (Freqtrade beats once a minute); `nav_points` gaps > 45 min are missed ledger ticks; `host_suspended` incidents in `ops_incidents` with the hours their detail carries |
| 2 | refused entries | count | `gate_decisions` with `allowed=0 AND intent='entry'`; rows with the same (sleeve, pair, reason) within 300 s are the 5-second retries of ONE refusal (the review collapsed 1,998 rows to 40 this way); by reason, with a plain cause |
| 3 | the funnel | count | `signals` by status → candidates, passed the screen, reached the validator (`signal_validations` join), got a usable verdict, proposed, gate-allowed, filled (`trades.open_date`); drop reasons: screened out, expired unvalidated, validator error, abstain, blocked, `skipped_capability` (`provider_switches`), `screen_unavailable` (on_all_failed), model call failures (`llm_calls`) |
| 4 | model spend for nothing | USD | `llm_calls` grouped by `(task, run_ref)` with no `ok` row; `signal_validations` that are `uncertain` with an `error` (an `ok` call that decided nothing); the `runs` table's cost on failed stages as the overlapping upper bound |
| 5 | decision-stage inputs | count | `knowledge/state/latest.json` (`assets`, `asof_candle_utc`, `data_fresh`, `newest_data_age_min`) and the proposals in the window whose rationale cites `assets={}` / `asof_candle_utc=null` |
| 6 | data staleness | minutes | the `flags` audit table walked in order (a set row opens an outage, the next clear row closes it; an outage still open ends at `expires_utc` or now — rows before 2026-09-25 stamped the clear row with the clear time, so pairing by `set_utc` is unsafe); `ops.lib.freshness.blocking_ages` at `until`, with the `candles_<tf>` one-bar allowance |
| 7 | holdings watcher | count | `logs/watch.log` JSON lines; a cycle is wrong when it reports `holdings: 0` while any run database had a trade open at that stamp |
| 8 | fee drag | USDT | fees ÷ Σ seed against `risk.max_fee_pct_per_month × hours / 720` |
| 9 | ledger integrity | count | `nav_points`: a tick whose `realized_pnl` is 0 and `nav` is the seed right after a tick whose `realized_pnl` was not 0; the cumulative-pot-minus-ledger gap now |
| 10 | operator and code events | USDT | `force_exit` / `target_zero` exits in the run databases, with the `audit_log` `autonomy.flatten` / `kill.engage` / `bot.forceexit` row within five minutes naming the hand |

### D. The top three

Each gap carries a **severity** (0-100: how much of the window or the money it took *within
its own scope*) and a **weight** (`GAP_WEIGHTS`: how much of the system that scope covers —
a host asleep stops both bots and every trade, 1.0; an empty decision input stops the AI
bot's proposals only, 0.7; a watcher that sees nothing loses monitoring, not money, 0.4).
The three sentences are the three highest `severity × weight`, written by the server in the
words the card prints, so the nightly markdown and Home never disagree.

---

## 2. Where it runs

* **Nightly**: `runs/daily_review.py` calls `profit_gaps.appendix_line(...)` in its
  postflight — one line, and the function never raises, so a ledger failure is a line in the
  appendix and never a failed review. Output: `reports/profit-gaps/<YYYY-MM-DD>.md` and
  `knowledge/state/profit_gaps.json` under the state root, rewritten whole (idempotent).
* **On demand**: `GET /api/profit-gaps` (`console/routers/profit_gaps.py` →
  `console/services/profit_gaps_service.py`), read-only connections, cached five minutes per
  state root, `?refresh=true` to recompute. A ledger that cannot be computed at all comes
  back as `error`, never a 500; a section that cannot be computed is a gap row with `error`.
* **Home**: `ProfitGapsCard` shows A, B (three numbers with their definitions) and D; C is
  behind one button and is rendered only while open. Home is five blocks by contract
  (`overview.test.tsx` fails on a sixth and bans the builder words), so the card sits inside
  the money block as its own full-width row, and every sentence the server writes avoids
  those words ("the rules bot", "the AI bot", "passed the screen", never "sleeve",
  "screened", "validated").

Read-only by construction: the run databases are opened `mode=ro`, the console opens the
journal and knowledge databases `mode=ro` (a test asserts it), and nothing writes into a
database anywhere in the ledger.

---

## 3. The first reading — 2026-09-29 13:47Z, against a copy of the live data

Computed against `/tmp/gl1`, a `sqlite3 backup` copy of the runtime databases and a copy of
the logs, with `now` set to the newest heartbeat in the copied log (13:47:02Z) so the copy's
own age is not counted as dark time, and the `fast-test` profile the live bots run.

**Last 24 hours.** Expected −1.29% per 30 days (−0.043% scaled), planned max drawdown −26.8%.
Realised **+48.04 USDT** net on 2 trades (AVAX, both bots, ROI exits), gross 50.09, fees
2.05 (4.1% of gross), open mark −20.12 (SOL, both bots); holding BTC would have made +104.74,
the 31-pair basket +137.31. The three gaps that mattered:

1. **You were not trading 22.0 of the last 24 hours**: no bot heartbeat 09-28 13:47Z → 09-29
   04:53Z and 06:13Z → 13:10Z (the host was asleep, then hibernated this morning). Uptime 8%.
2. **36 distinct entries were refused** (305 rows counting retries) against 4 allowed — 14 on
   `staleness`, 14 on `blackout:data_stale`, 8 on `beta_cap` — the gate refusing in the
   minutes after each wake-up.
3. **9 of 9 signals that passed the screen never got a verdict**: the validator errored on
   `unknown feature_key` on all 3 that reached it; 5 expired unvalidated. The AI bot's
   decision stage abstained 2 of 2 times on `assets={}`. 1.65 USD of validation spend bought
   nothing. The position watcher saw 0 holdings in 8 of 8 cycles while AVAX/SOL were open.

**Since the test began (144.2 h).** Realised **−12.05 USDT** net on 16 trades (50% won),
gross +19.93, fees **31.98 = 160% of gross**; cumulative pot 19,967.83; holding BTC −399.10,
the basket +1,074.42. Top three: fees took 160% of gross; not trading 109.7 of 144 hours
(uptime 24%); 64 of 64 screened signals never got a verdict. Also: 77 distinct refusals
(2,303 rows), 8 of 8 proposals abstained on empty inputs, 8.67 USD of model spend for
nothing (11.54 by the runs table), the data-stale flag active 20.3 hours, the watcher blind
in 100 of 103 cycles with a position open, the ledger reset twice hiding −69.77, and the two
day-one events at −69.77 (the hand-typed flatten on the rules bot −42.21 by `human:console`,
the no-mandate sell on the AI bot −27.56).

The full markdown with every line and every query is `reports/profit-gaps/2026-09-29.md`
once the daily review has run on the live host; the reading above is the copy's.

---

## 4. What this does not claim

* It does not measure return. Section A says why on every rendering, and nothing in B is
  presented as a verdict on the strategy.
* It does not price what a dark hour would have earned. `paper-trading-review` §2 bounds the
  week's forgone P&L at +30 to +192 USDT under optimistic fills and calls that unhonest to
  quote; the ledger reports hours, not imagined money.
* The `host_suspended` incident line reads a table another workflow is populating; until it
  lands the uptime gap says "no bot heartbeat was logged" rather than "the host was
  suspended". The measurement (dark hours from the logs) is the same either way.
* Severity weights are a code constant with a stated reason each (`GAP_WEIGHTS`), not a
  measured quantity. They decide which three sentences go first, nothing else.

---

## 5. Files

`runs/profit_gaps.py` (the ledger), `console/services/profit_gaps_service.py` (read-only,
cached), `console/routers/profit_gaps.py` (`GET /api/profit-gaps`),
`console/contracts.py: ProfitGapsResponse`, `console/web/src/pages/overview/ProfitGapsCard.tsx`
(+ test), `console/web/src/api/contracts.ts`, `runs/daily_review.py` (one line),
`tests/test_ops/test_profit_gaps.py`, `tests/test_console/test_profit_gaps_api.py`,
`docs/contracts.md` §9.6.
