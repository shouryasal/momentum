# Rate limits and recvWindow (Binance spot)

- Request weight: 6000 per minute per IP (headers `X-MBX-USED-WEIGHT-1M`). Earn's
  ingest budget is ~35 weight per 15-min run; the ingest aborts a phase above 50%
  of the cap as a guard.
- Typical weights: klines 2, depth(≤100) 5, exchangeInfo 20, account 20.
- Order rate: 100 orders / 10s and 200,000 / day per account — Earn's 4 trades/day
  cap is nowhere near it; relevant only if a bug loops order placement (the gate's
  trades/day check and the kill switch are the backstops).
- 429 → back off immediately; repeated → IP ban (418). Never retry-storm.
- `recvWindow`: keep ≤ 5000 ms; NTP-sync the host (WSL2: systemd-timesyncd).
  Error -1021 (timestamp outside recvWindow) means clock drift — fix time, don't
  raise recvWindow.
