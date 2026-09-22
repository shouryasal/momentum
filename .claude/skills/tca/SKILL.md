---
name: tca
description: Analyzes Earn execution quality — decision-time quote vs fill VWAP, fees and slippage in bps, threshold breaches, monthly cost calibration. Triggers on the weekly TCA report, tca, execution costs, slippage analysis.
allowed-tools: Read, Grep, Glob, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/report.py*)
---

# TCA (tier 2 — this skill is HUMAN-edit-only)

## Weekly report

```
python3 ${CLAUDE_SKILL_DIR}/scripts/report.py --out reports/tca-weekly.md
```

The script renders the numbers (per-sleeve 7d/30d fee/slippage/total bps, fill
counts, unreconciled count, calibration status, freeze verdict). Your job on top of
it: one paragraph of interpretation — what changed vs last week and whether the
cost trend threatens the < 1% NAV/month budget — citing only the script's numbers.

## The rules this skill documents (enforced elsewhere)

- Cost convention: `slippage_bps = side_sign x (fill - ref_mid)/ref_mid x 1e4`,
  fee converted to USDT (BNB at the covering 1h close); ref_mid from the fill
  row's decision-time quote, book-snapshot fallback within 900 s, else
  `unreconciled` (counted, alerted weekly).
- Monthly calibration: runs/tca_job.py writes prior-month medians into
  config/backtest.yaml when >= 20 fills exist. Described here, PERFORMED there.
- Freeze: measured > 1.5x assumption for 14 days -> `tier1_freeze` flag; recovery
  clears the clock, never the flag; `/unfreeze` (human) clears it once explained.
