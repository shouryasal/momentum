# Binance spot error codes worth knowing

| Code | Meaning | Handling |
|---|---|---|
| -1003 | Too many requests | Back off; check weight headers; never tighten the loop |
| -1013 | Filter failure (LOT_SIZE / PRICE_FILTER / NOTIONAL) | Size/round correctly; freqtrade handles rounding — a recurring -1013 is a bug report |
| -1021 | Timestamp outside recvWindow | Fix host clock (NTP), not recvWindow |
| -2010 | New order rejected (insufficient balance, market closed…) | Journal + alert; do not blind-retry |
| -2011 | Cancel rejected (unknown order) | Usually already filled/cancelled — reconcile via order status |
| 418/429 (HTTP) | Ban / rate limit | Stop all non-essential calls; healthcheck alerts |

Freqtrade's ccxt layer maps most of these to typed exceptions; the runbook's
"exchange outage" procedure applies when they persist.
