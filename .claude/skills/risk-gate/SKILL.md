---
name: risk-gate
description: Documents the deterministic risk limits Earn's callbacks enforce, the Freqtrade protections config, breach handling, and renders the weekly risk report. Triggers on risk report, limits, breaches, risk gate questions.
allowed-tools: Read, Grep, Glob, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/risk_report.py*)
---

# Risk gate (tier 2 — HUMAN-edit-only)

The limits live in `config/earn.yaml` and are enforced by `strategies/riskgate.py`
through freqtrade callbacks (`confirm_trade_entry`, `custom_stake_amount`,
`custom_entry_price`, `order_filled`, `bot_loop_start`) plus the protections
(CooldownPeriod 2 candles, StoplossGuard 3-in-48h -> 24h, MaxDrawdown equity).
This skill documents; it never sets a limit.

## Weekly report

```
python3 ${CLAUDE_SKILL_DIR}/scripts/risk_report.py --out reports/risk-weekly.md
```

The script asserts every limit it prints equals `config/earn.yaml` (anti-drift)
and lists the week's rejections/breaches per rule. Add one paragraph: which limits
were exercised, any breach root-caused (a breach = a sizing reject at
confirm-stage that cap_stake should have prevented — always a bug report).

## Breach handling

Gate breach row (severity='breach') -> healthcheck alerts immediately -> the
weekly review root-causes it (usually `strategy` or a code bug -> human).

## Stops and how each one ENDS

Every lock in the gate is bounded by the condition that set it. There is no
open-ended one; a lock that outlives its own reason is a bug, not a safety margin.

| Stop | Measured from | Ends |
|---|---|---|
| Daily -3% | the Gulf-DAY NAV anchor | `locked_until` = fire time + `daily_stop_lock_hours` (24h). A timestamp, compared against the CALLER's clock, so it holds for exactly 24 simulated hours in a backtest too. One timed freqtrade pair lock, same expiry. |
| Monthly -10% | the Gulf-MONTH NAV anchor | the end of that Gulf month. `loop_tick` re-anchors NAV and releases `monthly_locked` in the same tick; `monthly_locked_month` dates the lock so the read side (`check_entry`, `flatten_pending`) honours the expiry before the next tick. No freqtrade pair lock at all. |
| Consecutive stop-outs | `StoplossGuard` (3 in 48h) | `stop_duration_candles` = `stoploss_guard.lock_hours` (24h) in candles — freqtrade's own, candle-counted. |
| Re-entry after a stop/TP | `stopped_<pair>` stamp | `reentry_cooldown_hours`, or sooner if a newer proposal exists. |

**Monthly stop, per mode** (`GateConfig.monthly_lock_release`):

* **LIVE — `human_or_month_end`.** The sleeve is PAUSED, visibly, and a human may
  resume it early through `ops.lib.risk_resume.resume()` (which re-anchors NAV and
  drops the leftover pair locks). The human gate is a way OUT EARLY, not the only way
  out: if nobody resumes, the Gulf month boundary ends the pause by itself.
* **TEST / backtest — `month_end`.** No human exists, so the boundary is the only
  release. This is not a relaxation, it is the difference between measuring the
  strategy and measuring the lock: the flag used to be permanent, and a single -10%
  month in May 2021 silenced SleeveA for the remaining 5.3 years of a 2021→2026
  backtest (16 trades, all before 2021-05-22, then nothing).

`ops.lib.risk_resume.status()` reports `monthly_locked` as the EFFECTIVE state at
`now`, with `monthly_lock_flag_set` (the raw row) and `monthly_lock_expires_after`
(the month it dies with) beside it.

## Recorded deviation (spec §6 "market orders only for stop exits")

Freqtrade's stoploss / emergency_exit / force_exit paths ARE market orders.
Gate-initiated flattens go out as spread-crossing limit orders
(`custom_exit_price`), replaced up to `exit_timeout_count` times, THEN
emergency_exit (market). Accepted because price-modifying callbacks apply to limit
orders only; the fallback bounds the delay.
