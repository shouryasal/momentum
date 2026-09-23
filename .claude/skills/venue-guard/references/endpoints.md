# venue-guard — verified endpoints, limits and the state schema

Every URL below was called from this host on **2026-09-23** and the status, size and a
sample of the body recorded. Nothing here is quoted from documentation. A source that needs
a key is out; there are none on this page.

## The blocking source — the only one

| | |
|---|---|
| URL | `https://api.binance.com/api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]` |
| Key | none |
| Verified | HTTP 200, 10,245 B |
| Rate limit | the response's own block says `REQUEST_WEIGHT 6000/min` (not the often-quoted 1200); this call costs weight 4 |
| Cadence | 5 min |
| `max_lag` | 30 min — beyond that the symbol status is *not asserted* and does not block |

Sample (trimmed): both symbols `"status": "TRADING"`, `"isSpotTradingAllowed": true`,
`PRICE_FILTER.tickSize` 0.01 for both, `LOT_SIZE.stepSize` 0.00001 (BTC) / 0.0001 (ETH),
`NOTIONAL.minNotional` **5.0**. Note `risk.min_notional_usdt` is the stricter, binding one —
the exchange's 5.0 is never the constraint that actually bites.

## Quote-currency sources

| Name | URL | Verified 2026-09-23 | History | Cadence / max_lag |
|---|---|---|---|---|
| `coinbase_usdt_usd` | `https://api.exchange.coinbase.com/products/USDT-USD/ticker` | 200, bid 0.99975 / ask 0.99976 | candles: `?granularity=3600&start&end`, max 300 bars/req, hourly back to **~2021-06** (2020 returns 0 rows) | 15 min / 120 min |
| `bitstamp_usdt_usd` | `https://www.bitstamp.net/api/v2/ohlc/usdtusd/?step=3600&limit=1000` | 200; `limit=1500` → **HTTP 400** | hourly back to **2022-01-10** (2021 returns 0 rows) | 60 min / 180 min |
| `kraken_usdt_usd` | `https://api.kraken.com/0/public/Ticker?pair=USDTZUSD` | 200, bid 0.99978 / ask 0.99979 | OHLC serves only **~720 bars** — a live source, **not** a history source | 15 min / 120 min |
| `binance_usdc_usdt` | `https://api.binance.com/api/v3/klines?symbol=USDCUSDT&interval=1h` | 200 | hourly from **2018-12-15**, 64,094 bars pulled | 60 min / 180 min |
| `binance_fdusd_usdt` | `https://api.binance.com/api/v3/klines?symbol=FDUSDUSDT&interval=1h` | 200, close 0.99920 | hourly from **2023-07-26** | 60 min / 180 min |

**Why Bitstamp is on this list.** Kraken serves ~720 bars, so with Coinbase alone the
two-source confirmation rule could only ever be exercised live — it would have shipped
untested. Bitstamp gives a second USD venue *with history*, which is what makes the rule
backtestable: replayed hour by hour over 41,401 hours, the rule fires on exactly two
episodes and both are real.

## Best-effort source — may warn, never block

| | |
|---|---|
| URL | `https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=161&pageNo=1&pageSize=20` |
| Verified | HTTP 200, `catalogName: "Delisting"`, `total: 436` |
| Rate limit | **undocumented** — `bapi` is Binance's unversioned website API with no published limit |
| Lead time | recent notices carry 2–14 days ("Notice of Removal of Spot Trading Pairs - 2026-09-25", released 2026-09-22) |
| Cadence / max_lag | 60 min / 720 min |

`apex.binance.com` is not usable from this host — the same CMS path there returned **404**
on a final re-check (a research report recorded 403; either way it does not serve us, and
the honest record is that it failed, not which way). It is not used. Because the shape can
change without a version bump, every read passes through `cms_canary()`, which asserts
`code == "000000"`, the presence of catalog 161 and an `articles` list. A canary failure or
a 403 degrades to the existing RSS whitelist and **warns**; it can never block an order and
never fails the run. *The absence of an announcement is not an all-clear.*

Also verified: `https://api.binance.com/sapi/v1/system/status` → 200 `{"status":0,"msg":"normal"}`,
keyless. Informational only.

## Observe-only — `depth_at_peg`

`https://api.binance.com/api/v3/depth?symbol=USDCUSDT&limit=100` → 200, 6,273 B, 100 levels
each side. Verified 2026-09-23: **$15.3m of bids and $27.5m of asks within 50 bps of par.**

It is labelled `observe_only: true` and is barred from every rule, because **no free spot
order-book archive exists** — there is no history to replay it against, so a detector built
on it could never survive a backtest. It is reported because "the peg held on $15m of bids"
and "the peg held on $15k of bids" are different facts, and a reader should be told which.
The series accrues forward through Earn's existing `book_snapshots`.

## The peg rule, and the measurement that sets it

Three consecutive hourly **closes** more than **50 bps** off par, on **two independent USD
venues**. Each element is there because of a specific measurement.

*Persistence, and closes not lows.* The worst single printed low in Binance `USDCUSDT` daily
history is **0.7600 on 2024-01-03 — with a close of 0.9995**. On hourly bars that day has
**zero** closes more than 50 bps off par. The March 2023 event has **23 consecutive** hourly
closes past the band (worst close 0.9128 at 2023-03-11T15Z). Any detector keyed on a bar's
low fires on the wick; one keyed on a run of closes does not.

*The 50 bps level.* The wick produces a −2,400 bps low and a −5 bps close; March 2023
produced −872 bps closes. Any close-based threshold between roughly 15 and 500 bps separates
them, so 50 sits in the middle of a wide plateau rather than on an edge.

*Two sources.* Measured 2022-01 → 2026-09, per-source hourly deviation from par:

| source | n | mean | sd | min | max | bars past ±50 bps |
|---|---|---|---|---|---|---|
| `binance_usdc_usdt` | 37,445 | −0.05 bps | 16.91 | −872.0 | +500.0 | 94 |
| `binance_fdusd_usdt` | 27,724 | −9.14 bps | 12.78 | −350.0 | +100.0 | 92 |
| `coinbase_usdt_usd` | 41,426 | −0.99 bps | 7.62 | −280.0 | +149.7 | 85 |
| `bitstamp_usdt_usd` | 41,439 | −0.87 bps | 7.69 | −300.0 | +150.0 | 72 |

Single-venue excursions are common; simultaneous ones are not.

## What the replay found — 41,401 hours, 2022-01-02 → 2026-09-23

| state | hours |
|---|---|
| `ok` | 41,241 (99.61%) |
| `peer_depeg` | 66 |
| `usdt_premium` | 41 |
| `peer_premium` | 37 |
| **`usdt_depeg` (blocking)** | **9 (0.022%)** |
| `usdt_unconfirmed_high` | 4 |
| `usdt_unconfirmed_low` | 3 |

The blocking flag fires in **two episodes, both real**:

1. **2022-05-12T08Z → 15Z (8 hours) — Terra/UST.** Coinbase −160 to −170 bps and Bitstamp
   −87 to −196 bps at the same hours, while `USDCUSDT` printed **+221 bps**. The same fact
   from both sides: USDT was the thing being sold.
2. **2022-11-10T14Z (1 hour) — FTX.** Coinbase −53.5 bps, Bitstamp −63.4 bps. One hour,
   just past the band.

No wick ever fired. March 2023 came out as `usdt_premium` then `peer_depeg` — a warning,
never a block — which is correct: our unit of account was not what broke.

## The USDT-basis correction

Any cross-venue figure must divide the Binance price by a live USDT/USD rate **first**, or
it measures Tether. Over **8,749 hours** of Binance BTCUSDT against Coinbase BTC-USD
(2025-09-23 → 2026-09-23):

| | mean | median | p95 | max |
|---|---|---|---|---|
| raw dispersion | 6.04 bps | 4.90 | 15.10 | 37.29 |
| **corrected** | **1.14 bps** | 0.94 | 2.84 | 17.86 |

**81.2% of the apparent venue dispersion is the Tether basis.** USDT basis over the same
window: mean −4.29 bps, sd 5.83, range −24.9 to +50.7. Without the rate the figure is
suppressed, never published uncorrected.

## `knowledge/state/venue.json`

```
computed_utc            ISO-8601 Z
symbol_status.<SYM>     status, spot_trading_allowed, order_types, tick_size, step_size,
                        min_qty, min_notional, tradable
symbols_not_tradable    [] when everything is TRADING — the only blocking venue input
system_status_ok        bool, informational
peg.state               ok | usdt_depeg | usdt_unconfirmed_low | usdt_premium |
                        peer_depeg | peer_premium | usdt_unconfirmed_high |
                        degraded | no_data
peg.usdt_state          depeg | unconfirmed_low | premium | unconfirmed_high | at_par | no_data
peg.peer_state          depeg | premium | at_par | no_data
peg.depeg_flag          the ONLY peg field the gate acts on
peg.confirmable         false ⇒ a depeg could not be confirmed right now
peg.reads[]             per source: last_close, dev_bps, run_length, run_sign, n_bars, stale
usdt_usd_mid            median of the fresh USD venues (median, so one bad venue moves nothing)
usdt_basis_bps          (mid − 1) × 1e4
dispersion              raw and corrected, or null when no rate is available
depth_at_peg            resting USDCUSDT notional within 50 bps of par, observe_only:true
announcements           canary_ok, canary_detail, blocking:false, n_articles, delist_hits_24h
blocking[]              reasons an entry should be refused
warnings[]              everything else, including every staleness problem
sources.<name>          as_of, age_min, stale, error — one row per source, always present
```

## Refresh cadence

`scanner` runs it every cycle; each source refetches only when its own `cadence_min` has
elapsed, and the cache under `knowledge/cache/venue/` carries the `fetched_at` stamp that
decides. Nothing is ever refetched inside its cadence, and nothing is ever *used* past its
`max_lag_min` without a `stale: true` beside it.
