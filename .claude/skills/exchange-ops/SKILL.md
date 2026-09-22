---
name: exchange-ops
description: Background reference for Binance spot mechanics — symbol filters, rate-limit weights, order types, error codes and recvWindow — consulted when writing or reviewing execution-adjacent code and when order feasibility matters.
user-invocable: false
allowed-tools: Read
---

# Exchange ops (background knowledge)

<!-- Edits: Claude, free. Values in references/ are marked approximate and must be
re-verified against live exchangeInfo before live trading. -->

Reference map:

- `references/filters.md` — LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL,
  PERCENT_PRICE_BY_SIDE for BTC/USDT and ETH/USDT.
- `references/rate-limits.md` — request-weight model, order-rate caps, recvWindow.
- `references/errors.md` — error codes and the handling each deserves.

The one decision-relevant rule inline: **a rebalance leg below MIN_NOTIONAL cannot
execute** — when a proposal implies dust-sized legs, prefer `hold` over forcing a
trade the exchange will reject (the gate's min_notional check enforces this
anyway; do not fight it).
