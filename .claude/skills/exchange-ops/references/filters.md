# Symbol filters (VERIFY against live GET /api/v3/exchangeInfo before live trading)

Approximate values as of 2026-09; freqtrade's exchange layer enforces the live
values automatically — this reference exists so code review can sanity-check sizes.

| Filter | BTC/USDT | ETH/USDT |
|---|---|---|
| LOT_SIZE stepSize | 0.00001 BTC | 0.0001 ETH |
| LOT_SIZE minQty | 0.00001 BTC | 0.0001 ETH |
| PRICE_FILTER tickSize | 0.01 | 0.01 |
| NOTIONAL minNotional | 5 USDT | 5 USDT |
| PERCENT_PRICE_BY_SIDE | ~±5x weighted avg | ~±5x weighted avg |

Earn's own `min_notional_usdt: 25` (earn.yaml) sits deliberately above the
exchange minimum — dust legs are refused by the gate before Binance ever sees them.
