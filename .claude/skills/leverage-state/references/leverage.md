# Leverage state — definitions, sources, and the evidence

Everything here was measured on this host against real data. Where a number came from the
design study rather than from a run of this skill's own code, it says so. Where this skill's
code **disagrees** with the design study, the disagreement is recorded rather than smoothed
over — §5 is the important section.

---

## 1. Sources

All free, all keyless, all verified with a real request on 2026-09-23.

| Feature group | Endpoint / file | Verified | Rate limit |
|---|---|---|---|
| Funding history | `fapi/v1/fundingRate?symbol=&startTime=&limit=1000` | 200; paged to **2019-09-10**, **7,711 prints** | weight 1, 1000 rows/page |
| Funding live | `fapi/v1/premiumIndex?symbol=` | 200 | weight 1 |
| Funding caps | `fapi/v1/fundingInfo` | 200; BTCUSDT and ETHUSDT both **±0.00300 / 8h** | weight 1 |
| Open interest live | `fapi/v1/openInterest?symbol=` | 200 | weight 1 |
| OI + positioning history | `data.binance.vision/data/futures/um/daily/metrics/<SYM>/<SYM>-metrics-<date>.zip` | BTCUSDT **2020-09-01**+ (2020-08-31 → 404), ETHUSDT **2021-12-01**+ (every November 2021 day → 404) | CDN, none published |
| Book depth | `…/daily/bookDepth/<SYM>/…` | 2023-01-01, 28,560 rows | CDN |
| Perp klines | `fapi/v1/klines?symbol=&interval=&limit=1500` | 200; 1d reaches **2019-09-08** | weight 2–5 |
| Spot candles | local `data/binance/<PAIR>-<tf>.feather` | BTC 1d 3,324 rows, 2017-08-17 → 2026-09-22 | local |

**Refresh cadence.** Funding settles every 8h, so the funding cache refreshes on any run and is
stale past 16h. The metrics archive publishes **T-1** (on 2026-09-23, `2026-09-22` was a 200 and
`2026-09-23` a 404), so the archive is stale past 48h and a same-day reading is *expected* to be
a day old. Backfill: `python -m runs.features.binance_archive metrics BTCUSDT ETHUSDT`.

**Deliberately not used: `fapi/…/futures/data/*`.** `openInterestHist` with `limit=500` and no
`startTime` returns 31 rows; with a `startTime` one year back it returns HTTP 400
`parameter 'startTime' is invalid`. Every endpoint in that family retains ~30 days, so a
backtest built on them silently has a one-month sample. Deep history comes only from the bulk
archive.

**Not available free, verified by failure:** historical liquidations (`allForceOrders` → 404,
`forceOrders` → 401 account-scoped, archive `liquidationSnapshot` → 404, CoinGlass paid). The
public `!forceOrder@arr` websocket is free but forward-only and unbacktestable. Cascades are
therefore **inferred** from OI + price and labelled `source: oi_price_proxy`.

---

## 2. The central table — funding predicts the tail, not the return

Computed by this skill's own code over the full sample: 7,711 funding prints joined to BTC
daily closes and lows, **n = 2,562 daily anchors**, 2019-09-11 → 2026-09-15. Forward 7-day
drawdown is `min(low[t+1 … t+7]) / close[t] − 1`.

| 3d funding (ann %) | n | median 7d return | median 7d drawdown | **P(7d dd < −8%)** |
|---|---|---|---|---|
| < 0 | 289 | **+1.50%** | −3.82% | **17.0%** |
| 0–5 | 604 | +0.29% | −3.88% | 18.4% |
| 5–10 | 680 | +0.07% | −3.54% | 22.8% |
| 10–20 | 655 | +0.18% | −4.42% | 24.3% |
| 20–40 | 165 | +0.54% | −5.59% | 32.1% |
| ≥ 40 | 169 | **+1.41%** | −6.23% | **41.4%** |
| **all** | **2,562** | **+0.41%** | −4.23% | **23.3%** |

Read the return column before anything else. The **highest** funding bucket has the
**second-highest** median 7-day return. "High funding means overheated, sell" is backwards, and
on a long-only spot book it is not even an exit signal. The tail column is the entire finding:
it rises monotonically from 17.0% to 41.4% while returns stay flat-to-positive.

Split-half on the fixed ≥20% cut, split at 2023-03:

```
H1 2019-09 → 2023-03   P(dd<−8%) = 39.9%  vs base 30.5%   ratio 1.31
H2 2023-03 → 2026-09   P(dd<−8%) = 23.0%  vs base 16.3%   ratio 1.41
```

Absolute levels fell as the market calmed; the ratio held. That is why every threshold in the
code is a **rolling percentile** and never a fixed level.

---

## 3. Why three buckets cut at the 33rd and 80th percentiles

Re-fitting the table as *equal-count* buckets on a rolling window is a harder estimation
problem than the fixed-level table above, and running every (window, bucket-count) pair over
the real history says so. `p_dd` by bucket, and whether it came out monotone:

| window / buckets | p_dd by bucket | monotone |
|---|---|---|
| 730d / 3 | 0.135, 0.189, 0.148 | no |
| 730d / 5 | 0.116, 0.192, 0.137, 0.192, 0.151 | no |
| 1095d / 5 | 0.142, 0.169, 0.164, 0.201, 0.151 | no |
| 1825d / 5 | 0.178, 0.167, 0.178, 0.261, 0.196 | no |
| 1825d / 3 | 0.172, 0.189, 0.243 | **yes** |
| full / 3 | 0.180, 0.237, 0.308 | **yes** |

Quintiles are noise at every window short of the full sample. The arithmetic: a base rate near
0.17 on ~220 anchors per quintile has a standard error of ~2.5pp, so adjacent quintiles
differing by 3pp carry nothing — and because the 7-day forward windows overlap, 220 rows is
really about 31 independent observations.

Equal-count buckets were then wrong for a second reason: **they dilute the tail.** Walking the
rolling window point-in-time over 2,256 daily anchors:

| condition | fires | P(dd < −8%) | vs else | ratio (90% CI) |
|---|---|---|---|---|
| top rolling tercile | 559 | 25.2% | 21.6% | 1.17 |
| fund ≥ rolling q80 | 236 | 33.5% | 21.2% | **1.58 [1.13, 2.08]** |
| fund ≥ rolling q90 | 104 | 45.2% | 21.4% | 2.11 [1.38, 2.84] |

The effect lives in the top fifth, not spread evenly. A tercile top bucket starts at the 67th
percentile and mixes the genuinely crowded with the merely elevated — that is how a 1.58
becomes a 1.17. q90 is stronger still but fires ~100 times in six years (effective n ≈ 14),
too thin to calibrate a scalar on. So the cuts are the **rolling 33rd and 80th percentiles**.
Walked monthly point-in-time, that scheme is monotone on **60 of 64** dates; the isotonic
guard handles the other four and `monotone_raw: false` reports them.

---

## 4. The exposure multiplier

```
funding_mult  = base_rate / p_dd          capped at 1.0, live only while edge_live
quadrant_mult = 1 / quadrant_ratio        applied only while in price_down_oi_up
multiplier    = clip(min(legs), 0.40, 1.0)
```

Both legs are **measured ratios, not chosen constants**, so the haircut decays with the effect
instead of outliving it. Both are capped at 1.0 before they combine, so a missing, stale or
absurd input can only leave the multiplier at 1.0 — it can never manufacture a buy. The 0.40
floor stops a risk input from flattening the book: this is a sizing feature, not an exit.

---

## 5. The finding that changed the design — the edge is not currently measurable

This is the part that disagrees with the design study, and it is the reason this skill has a
switch the study did not specify.

Walking the table point-in-time, monthly, over 64 dates, the tail-to-baseline ratio:

```
2021-06 → 2022-01   1.29 – 1.48      2023-01 → 2026-01   1.27 – 1.50
2026-02   1.19      2026-03   1.18      2026-04   1.15      2026-05   1.13
2026-06   0.99      2026-07   0.96      2026-08   0.98      2026-09   1.02
```

The ratio decayed through 2026 and is now indistinguishable from 1.0. The block-bootstrap
lower bound agrees (currently **0.72**), and so does the split-half: whichever construction is
used, the second half's interval includes 1.0 (rolling q80 H2 ratio 1.08, CI [0.48, 1.85];
fixed ≥20 H2 ratio 1.44, CI [0.67, 2.41] — both with effective n of 8–11, which is why neither
can settle it).

This is the same shape as the design study's own cautionary tale — OI deleveraging as a buy
signal *worked and then died*, and a full-sample backtest would have shipped a corpse. A
full-sample average here would keep sizing on a relationship that 2020–2023 is carrying.

So the code measures the tail ratio **on the same rolling window the buckets come from**, with
a block bootstrap whose block length is the forward horizon, and the funding leg is live only
while the interval's lower bound clears 1.0. Verified behaviour at replay dates:

| date | tail ratio | CI lower | edge_live | multiplier |
|---|---|---|---|---|
| 2021-11-10 | 1.40 | 1.14 | true | **0.712** |
| 2022-08-01 | 1.21 | 0.90 | false | 1.000 |
| 2024-03-14 | 1.36 | 1.10 | true | **0.737** |
| 2025-03-01 | 1.44 | 1.24 | true | 1.000 (mid bucket) |
| 2026-03-01 | 1.18 | 0.88 | false | 1.000 |
| 2026-09-23 | 0.97 | 0.74 | false | 1.000 |

The two dates where the multiplier bites hardest are the two that immediately precede a major
drawdown. Today the feature has stood itself down, correctly.

**What this means for a run.** When `edge_live` is false the skill still reports every feature
— they are real measurements and the evidence pack wants them — but it sizes on none of them.
Report the multiplier as 1.0 and say the edge is not currently measurable. Do not argue around
it.

---

## 6. Loader traps, and what each one would have corrupted

All four confirmed against real files, all four handled in `runs/features/`.

1. **The metrics grid is neither uniform nor sorted.** `BTCUSDT-metrics-2026-09-20.csv` has 288
   rows spanning 00:30 → 23:55 with gaps of `{5: 76, 10: 48, 15: 42, 20: 37, 25: 19, 30: 20, …}`
   **and three negative gaps of about −1,400 minutes**. `BTCUSDT-metrics-2020-09-01.csv` has 576
   rows — every row duplicated exactly once. A loader assuming 5-minute spacing or monotone time
   produces garbage. Handled: sort, drop duplicate timestamps, reindex onto an explicit grid
   with a bounded forward fill so real outages stay visible as NaN.
2. **Milliseconds vs microseconds.** Spot klines in the archive switched to microseconds at
   2025-01; futures klines did not; REST is milliseconds everywhere. Handled by branching on
   **digit count**, never on a date.
3. **Near-zero open interest → `inf` on `pct_change`.** Measured over the full 2,213-day BTCUSDT
   cache: **10 rows** carry `sum_open_interest` at or below 1 contract (raw minimum 0.0), and a
   **separate 12 rows** carry `sum_open_interest_value` of exactly 0.0 while the contract count
   is a normal 106,675 (2023-04-10 08:25 onward). Guarding only the contract column would have
   left those twelve zeros to produce an `inf` the moment anything took a percentage change of
   notional. Each column is judged on itself.
4. **`markPrice` is an empty string on early `fundingRate` rows.** Measured: **4,536 of 7,711**
   BTCUSDT prints have no mark price. A naive `float()` raises; a naive `astype` yields NaN that
   then poisons a mean. The row is kept and the column coerced — `fundingRate` is present and is
   the only column any feature reads.

---

## 7. Field reference

`knowledge/state/leverage.json`, per pair.

| Field | Meaning |
|---|---|
| `funding.fr_8h` | Most recent 8h funding rate, as a decimal |
| `funding.funding_ann_3d` | Trailing 3-day mean funding, annualised, in percent |
| `funding.funding_pctile_1y` | Its percentile within the trailing year. Reported, decides nothing |
| `funding.fr_sign_run_length` | Consecutive same-sign prints, signed |
| `funding.fr_capped_flag` | Rate at Binance's adjusted cap — a **censored** observation |
| `open_interest.oi_chg_24h` | 24h change in contracts |
| `open_interest.oi_z_180` | That change, z-scored on 180 days |
| `open_interest.oi_notional_to_adv` | OI notional over 30d average spot dollar volume |
| `open_interest.quadrant` | `sign(price 24h) × sign(OI 24h)`. `price_down_oi_up` is the dangerous one |
| `basis.basis_bps`, `basis_z_180` | Perp–spot basis. **Observe-only** |
| `positioning.top_vs_global_spread` | Top traders minus everybody. **Observe-only** |
| `cascade.*` | Forced-selling proxy from OI + price. Never a liquidation measurement |
| `drawdown_table.*` | The fitted table, its cuts, `n_effective`, `monotone_raw`, `tail_ratio`, `edge_live` |
| `p_drawdown_7d` | `P(7d dd < −8%)` for the current bucket. **Never quote without `p_drawdown_base_rate`** |
| `exposure_multiplier` | The tightening-only scalar, in `(0, 1]` |
| `no_direction` | Always `true`. This skill has no direction to give |

### Why the perp basis is observe-only

Mean basis on Binance is **−1.38 bps** (sd 6.88) — the perp trades slightly *below* spot on
average, so "perp trades rich, basis > 0 is bullish" is wrong on this venue and any raw-level
rule inherits the error. The tail is interesting: basis below −10 bps (79 events in 7 years)
preceded a forward 72h return of +2.76% against a +0.39% baseline **with forward realised vol
of 1.083 against ~0.50**. A rare, violent buy-the-panic marker that arrives with double the
normal volatility — exactly what a drawdown-averse book sizes *down* into even when the
direction is right. 79 events, not split-half tested: hypothesis, not fact, and nothing sizes
on it.
