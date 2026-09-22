# Market-state definitions (pinned)

| Quantity | Definition |
|---|---|
| Trend | 1d close vs 200d SMA with 1% hysteresis: `up` above MA×1.01, `down` below MA×0.99, else `flat` (previous value NOT carried — flat is reported as flat) |
| Realized vol | stdev of 1d ln-returns over 20 days × √365 |
| Vol regime | `low` < 0.30 ≤ `med` < 0.60 ≤ `high` (annualized) |
| Drawdown | last close / max(close, 90d window) − 1 |
| Funding | latest 8h funding rate from the perp premium index (context only — Earn trades spot) |
| Breadth | share of universe assets with trend `up` (2-asset universe: 0, 0.5 or 1 — a recorded deviation from the spec's broad-market breadth) |
| Regime | `high_vol` when max asset vol is high; else `trend_up`/`trend_down` by BTC's trend; else `range` |
| regime_changed_utc | timestamp of the last regime VALUE change (carried forward while unchanged) — feeds the 48h hard-case escalation flag |
| disagreement | trend module long while vol regime high — feeds escalation |
| data_fresh | newest book snapshot ≤ 30 min old and newest 1h candle ≤ 30 min past its natural age |
