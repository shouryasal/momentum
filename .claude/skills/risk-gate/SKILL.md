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
Daily stop -3%: flatten + 24h lock (automatic resume). Monthly stop -10%: flatten +
`monthly_locked` in risk_state — resume is HUMAN-ONLY via the ops-runbook.

## Recorded deviation (spec §6 "market orders only for stop exits")

Freqtrade's stoploss / emergency_exit / force_exit paths ARE market orders.
Gate-initiated flattens go out as spread-crossing limit orders
(`custom_exit_price`), replaced up to `exit_timeout_count` times, THEN
emergency_exit (market). Accepted because price-modifying callbacks apply to limit
orders only; the fallback bounds the delay.
