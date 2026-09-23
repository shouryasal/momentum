# Crypto research and skill design

**Status:** design proposal, not yet built. **Date:** 2026-09-23.
**Question asked:** *"create additional skills if needed based on what impacts crypto since crypto
is not like normal stocks — its techniques, ML logics and research should be different and unique."*

Five researchers swept microstructure, on-chain, quant/ML, events and the Binance venue surface.
They verified roughly 100 endpoints with live requests and ran original studies rather than citing
them. This document is the lead's decision on what Earn should actually gain.

---

## 0. The answer in one paragraph

Crypto is not different because it needs different ML. It is different because it **publishes its
leverage for free**. Perpetual funding, open interest, the perp–spot basis, and a continuously
quoted options surface are all public, keyless and historical — equities publish none of this. That
public leverage record is genuinely predictive, and it predicts the **second moment, not the first**:
volatility and drawdown, reliably; returns, unreliably or not at all. Every strong result in this
study has that shape, and every result that tried to make it directional either failed outright or
worked and then died. That is the correct shape for a system whose stated edge is smaller drawdowns
rather than faster gains. So the skills below forecast risk, size against it, and gate on it. **None
of them produces a direction.**

Two constraints decide what is buildable and both were verified here, not assumed:

1. **Every `fapi/…/futures/data/*` endpoint retains ~30 days.** Verified: `openInterestHist` with
   `limit=500` and no `startTime` returns exactly **31** daily rows; with a `startTime` one year back
   it returns HTTP 400 `{"msg":"parameter 'startTime' is invalid.","code":-1130}`. A backtest built
   on these endpoints silently has a one-month sample. Deep history exists **only** in the free
   `data.binance.vision` bulk archive.
2. **At Earn's order size the fee is the entire execution bill.** Measured by walking the live
   1000-level book (below). Any skill that optimises *when* to execute is optimising a rounding
   error.

---

## 1. Drivers that survived scrutiny

Verification status is graded honestly:
**L** = lead re-ran the study independently in this session ·
**R** = researcher-verified with a real request and an original study, lead verified the endpoint only ·
**E** = endpoint verified, edge *not* established.

### 1.1 Deribit DVOL → forward realised volatility — the strongest result in the study (L)

I replicated this from scratch on a different construction than the researcher used (daily bars, my
own DVOL pull, local feather prices) and it got **stronger**, not weaker.

```
DVOL daily 2021-03-24 → 2026-09-23 (2,010 points), joined to BTC 1d closes, n = 2,002 days
target = forward 7-day realised vol (annualised %), Newey-West lag 7

  trailing 30d realised vol alone   R² = 0.1665   NW-t = +7.72
  DVOL alone                        R² = 0.2680   NW-t = +10.86
  both together                     R² = 0.2682   DVOL t = +7.95 , trailing t = -0.33
```

**DVOL subsumes trailing realised vol entirely.** In the joint regression the trailing-vol
coefficient goes slightly negative and completely insignificant. Both sample halves hold
(H1 2021-03→2023-12: R² 0.264, t +8.39; H2 2023-12→2026-09: R² 0.154, t +6.48 — decayed but alive).
Quintiles are monotone with a large spread:

| DVOL quintile | mean DVOL | mean forward 7d realised vol |
|---|---|---|
| Q1 | 38.5 | 33.0 |
| Q2 | 47.8 | 41.3 |
| Q3 | 55.7 | 46.1 |
| Q4 | 67.5 | 53.8 |
| Q5 | 90.3 | 69.3 |

**A design finding my replication surfaced that no researcher stated.** DVOL is an *implied* index and
sits systematically above realised vol — the mean variance risk premium over this sample is
**+8.09 vol points**. The fitted mapping is:

```
sigma_hat_7d  =  6.28  +  0.707 × DVOL          (refit on an expanding window)
```

Substituting **raw** DVOL into a `target_vol / sigma` denominator therefore over-states volatility by
**+13% at DVOL 35, +23% at DVOL 60, +29% at DVOL 90** — a silent 13–29% position haircut that grows
exactly when you are most confident. The skill must ship the **fitted** mapping, not the raw index.

* Source: `https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&start_timestamp=<ms>&end_timestamp=<ms>&resolution=86400` — free, no key, no account. Returns `[ts,open,high,low,close]`. **Verified 200 this session.** Max ~1,000 points per request; page backwards. `resolution` accepts 60/3600/43200/86400. Send a browser `User-Agent` — Deribit resets the connection on python-urllib's default.
* **ETH caveat, verified:** `currency=ETH` works but a request spanning 2021-03→2026-09 returned points only from **2023-12-27**. ETH has materially less DVOL history than BTC. The skill must fall back to a HAR forecast for ETH where DVOL is absent, and say which one it used.
* Current level for the record: BTC DVOL **37.75**, i.e. bottom-quintile calm.
* Decay watch: this is a structural hedging-cost mechanism, not a crowded trade, so it should not
  arbitrage away — and it did not across halves. The real fragility is **operational**: if Deribit's
  BTC options liquidity migrated, DVOL would go *stale rather than wrong*, which is the dangerous
  failure. Log Deribit BTC option total open interest from `get_book_summary_by_currency` alongside
  DVOL and alert on a 50% fall from its 90d median; alert on rolling 250d OOS R² below 0.12.

### 1.2 Perpetual funding → forward drawdown, never forward return (L)

Also replicated independently, on the full 7-year funding history pulled fresh this session.

```
7,711 funding prints 2019-09-10 → 2026-09-23 (REST, paged), 3-day mean annualised,
joined to BTC daily closes/lows, n = 2,561 days
```

| 3d funding (ann %) | n | median 7d return | mean 7d return | median 7d drawdown | **P(7d dd < −8%)** |
|---|---|---|---|---|---|
| < 0 | 289 | **+1.50%** | +2.77% | −3.82% | **17.0%** |
| 0–5 | 604 | +0.29% | +0.72% | −3.88% | 18.4% |
| 5–10 | 680 | +0.07% | +0.38% | −3.54% | 22.8% |
| 10–20 | 654 | +0.19% | +1.02% | −4.43% | 24.3% |
| 20–40 | 165 | +0.54% | −0.13% | −5.59% | 32.1% |
| ≥ 40 | 169 | **+1.41%** | +1.18% | −6.23% | **41.4%** |
| **all** | 2,561 | +0.42% | +0.92% | −4.23% | 23.3% |

Read the return column carefully: the **highest** funding bucket has the **second-highest** median
7-day return. "High funding means overheated, sell" is *backwards*, and on a long-only spot book it
is not even an exit signal. What funding predicts is the **tail**: drawdown probability rises
monotonically from 17.0% to 41.4% and median drawdown deepens monotonically.

Split-half stability, which is the part that matters:

```
H1 2019-09 → 2023-03   funding ≥ 20 ann:  P(dd<−8%) = 39.9%  vs base 30.8%   ratio 1.30
H2 2023-03 → 2026-09   funding ≥ 20 ann:  P(dd<−8%) = 23.0%  vs base 15.8%   ratio 1.46
```

Absolute levels fell as the market calmed, but **the ratio held and strengthened**. This is the
signal to build on, and it must be expressed as a *rolling percentile*, never a fixed threshold,
because the level is compressing as the basis trade institutionalises.

* Source: `https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime=<ms>&limit=1000` — free, no key, weight 1, and unlike `futures/data/*` it serves **full history**. **Verified this session**: paged back to 2019-09-10, 7,711 rows. Note `markPrice` is an empty string on early rows — the loader must tolerate it.
* Live: `fapi/v1/premiumIndex?symbol=BTCUSDT` — verified, returns `lastFundingRate`, `markPrice`, `indexPrice`, `nextFundingTime`.
* Caps: `fapi/v1/fundingInfo` gives `adjustedFundingRateCap/Floor` and `fundingIntervalHours`. A capped rate is a **censored observation** and must not be treated as linear.
* **Correction to a researcher claim:** the monthly bulk funding archive does *not* reach 2019-09. Verified: `…/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2019-09.zip` and `-2019-10.zip` both **404**; `2020-01` returns 200 (93 rows). Use the REST endpoint for funding history — it is deeper and simpler.

### 1.3 Open-interest crowding → forward drawdown (R)

Price falling *while* open interest rises means longs are averaging down into weakness and the pool
of forced sellers is growing; the liquidation engine then becomes a price-insensitive market seller.
Researcher study, 6,592 4h bars 2023-09→2026-09, Binance bulk metrics joined to local candles:

* worst quadrant (price ↓ **and** OI ↑): forward 72h max drawdown **−2.60% vs −2.26%** baseline;
  p10 tail **−7.11% vs −5.85%**.
* Stable across halves (H1 −2.77 vs −2.36; H2 −2.44 vs −2.16). Ratio ≈ 1.13 in both.
* `OI 24h > +8%` → forward 72h drawdown −2.93% vs −2.26%.
* NW(18) regression of r72 on standardised 24h OI change: β = −0.241%/1sd, t = −2.88.

The **return** leg of the same story is dead and that is the most instructive result in the study —
see §2.

### 1.4 Order-book depth → forward realised volatility (R)

Volatility is order flow divided by depth; this is closer to an accounting identity than an edge, so
it cannot be arbitraged away. Researcher study on 1,440 4h bars from the Binance bulk bookDepth
archive: NW(6) regression of forward 24h realised vol on standardised log depth(±1%) gives
β = −0.081, **t = −7.50**, with Q1 depth \$241m → 0.501 annualised vol against Q5 depth \$470m →
0.280. An **1.8× volatility difference from a purely observable non-price variable.**

Two honest caveats: the test window is 8 months, and it uses *futures* depth as a proxy for *spot*
depth because **no spot bookDepth archive exists**. Verified free and available:
`https://data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-2023-01-01.zip`
→ HTTP 200, 463 KB, **28,560 rows**, columns `timestamp,percentage,depth,notional` at ±1/2/3/4/5%.
Earn already snapshots live spot depth into `book_snapshots`, so the spot series accrues forward.

### 1.5 Perp–spot basis and backwardation (R)

Mean basis on Binance is **−1.38 bps** (sd 6.88) — the perp trades slightly *below* spot on average,
so the folk prior "perp trades rich, basis > 0 is bullish" is wrong here and any threshold must be
set on a z-score, never a raw level. The tail is where the content is: basis < −10 bps (n=79 over 7
years) → forward 72h return +2.76% vs +0.39% baseline, **with forward realised vol 1.083 vs ~0.50** —
a rare, violent, buy-the-panic marker that arrives with double the normal volatility, which is
exactly the trade a drawdown-averse system must **size down into even when it is directionally
right**. 79 events, not split-half tested: hypothesis, not fact.

### 1.6 Stablecoin depeg — the unit of account, not a signal (L, endpoints)

Every Earn position is quoted in USDT. A USDT depeg is not a trade signal, it is a **measurement
failure**: a 2% USDT discount makes BTC/USDT print 2% higher with no change in BTC value, and it
silently re-scales NAV, the risk limits, the stop distances and the benchmark at once.

Verified live this session: `api.binance.com/api/v3/klines?symbol=USDCUSDT&interval=1h` → 200
(close 1.00010, hourly quote volume \$325m — a real liquid peg reference on the venue we trade);
`api.exchange.coinbase.com/products/USDT-USD/ticker` → 200, bid **0.99978** / ask 0.99979.

The detector design is decided by a researcher's measurement, and it is the kind of detail that
decides whether a hazard detector is useful or noise: the **worst single printed low** in Binance
USDCUSDT daily history is **0.7600 on 2024-01-03 — with a close of 0.9995.** That is a liquidation
wick. The real March 2023 event shows as **four consecutive depressed daily closes**
(0.9592 → 0.9848 → 0.9949 → 0.9961). **Persistence across closes plus depth is mandatory; any
detector keyed on a bar's low will fire on wicks.**

A second finding rescues a whole family of numbers from being wrong. Simultaneous reads gave Binance
BTC/USDT 85,607 against Coinbase BTC-USD 85,458 and Kraken 85,458 — a ~150 USD gap that *looks* like
17 bps of venue dispersion. Fetching the stablecoin leg showed the gap **is the Tether basis**
(Kraken USDT/USD 0.99969, Coinbase 0.99972); the two USD venues agree with each other to within
\$0.80. **Any cross-venue number must divide the Binance price by a live USDT/USD rate first, or it
just measures Tether.**

### 1.7 Macro events are volatility events with no direction — and the shipped blackout is too short (R + L endpoint)

Researcher event study, 68 FOMC statements 2019-01→2026-09 anchored at 14:00 ET with DST handling,
against 67,683 hourly BTC bars:

* **Volatility:** |T→T+4h| median 0.838% = **1.81×** the unconditional 0.464%; P(|4h return| > 2%) =
  **21% vs 9%** unconditionally.
* **Direction:** T→T+1h mean −0.22% (sd 1.14%); T→T+24h mean −0.27% (sd 3.18%). **Neither is
  significant.** You are being offered double the variance for zero expected return, before fees and
  before the wider spread. Standing aside is the positive-expectancy action.
* **The window profile, hour by hour, as a multiple of unconditional |1h| return:**
  T−7h 1.51× · T−2h 1.72× · **T−1h 2.26× (peak)** · T 1.55× · T+2h 1.58× · T+6h 1.50× · T+8h 1.19× ·
  T+9h 0.89× (back to baseline).

`config/earn.yaml: risk.blackout.window_minutes: 60` is symmetric and therefore covers only the peak
hour, leaving the entire elevated shoulder open on both sides. **The data supports roughly
−360 / +480 minutes.** This is a one-line config finding worth more than most new signals.

Sources verified by me this session: `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`
→ HTTP 200, and `re.findall(r'monetary(\d{8})a\.htm')` yields **47 statement dates** spanning
2021-01-27 → 2026-09-16 (historical pages extend this to 2012). The JSON endpoint
`/json/ne-fomccalendar.json` is a 404 that returns an HTML body — the kind of thing that silently
poisons a parser. For CPI: **every `www.bls.gov` path returns 403 from this host**, but
`https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0` works **keyless** (verified 200,
returned `{"year":"2026","period":"M08","latest":"true","value":"334.980"}`). That is *post-hoc*
release detection, not forward scheduling — enough to **audit** a hand-maintained calendar, not to
build one. So `config/macro_calendar.yaml` stays human-maintained and a **staleness alarm is
mandatory, not optional**.

### 1.8 Venue state — the only authoritative "can this order be filled" (L)

Verified this session: `api.binance.com/api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]` → 200,
both `status: TRADING`, `isSpotTradingAllowed: true`, and the response's own rate-limit block
confirms **REQUEST_WEIGHT 6000/min** (not the often-quoted 1200). The Binance CMS announcements
endpoint is also live: `…/cms/article/list/query?type=1&catalogId=161` → 200, catalog **"Delisting",
436 articles**, most recent titles carrying a **2–14 day lead time** ("Notice of Removal of Spot
Trading Pairs - 2026-09-25", released 2026-09-22).

Caveat recorded rather than buried: `bapi` is Binance's **undocumented, unversioned website API**
with no published rate limit, and the `apex` host returns 403. It is best-effort with a canary and a
fallback to the RSS whitelist. Only `exchangeInfo` — documented and stable — may **block**.

### 1.9 Structural facts about this specific two-asset portfolio (R)

* **BTC/ETH is a one-factor portfolio and it gets more so exactly when it matters.** Correlation
  0.784 full sample, **0.854** when BTC is >20% off its high (vs 0.579 otherwise), and ETH's beta to
  BTC rises to **1.17** on BTC's worst-5% days. Effective number of assets = 2/(1+ρ) = **1.12**.
  50/50 BTC+ETH scores Sharpe 0.81 / MaxDD −87.9% against 100% BTC at 0.83 / −83.2% — the
  "diversified" portfolio is strictly worse. **`risk.max_weight: {BTC: 0.40, default: 0.30}` is not
  diversification; it is decoration.** Sizing must be at portfolio level.
* **Deciding at 4h instead of 1d buys nothing and costs double.** Identical rule: 1d → Sharpe 1.14,
  turnover 12×/yr, fee drag 1.2%/yr. 4h → Sharpe 1.13, turnover 26×/yr, fee drag **2.6%/yr**, and a
  *worse* drawdown (−39.9% vs −34.0%). Against `risk.max_fee_pct_per_month: 0.01` (12%/yr headroom),
  the 4h cadence spends 22% of the annual budget for zero measured benefit.

### 1.10 Execution cost — measured twice, independently (L)

I walked the live 1000-level book myself because this single measurement kills an entire family of
proposed skills:

```
BTCUSDT  mid 84,662.01   spread 0.0100 = 0.0012 bps
   $  1,000  slip vs mid  0.001 bps        $ 100,000  slip vs mid  0.001 bps
   $ 25,000  slip vs mid  0.001 bps        $ 500,000  slip vs mid  0.193 bps
ETHUSDT  mid  2,681.40   spread 0.0100 = 0.0373 bps
   $  1,000  slip vs mid  0.019 bps        $ 100,000  slip vs mid  0.019 bps
   $ 25,000  slip vs mid  0.019 bps        $ 500,000  slip vs mid  0.873 bps
```

Against `exchange.fee_bps_assumed: 10`, slippage at every size Earn will ever trade is **four to five
orders of magnitude smaller than the fee**. Live seeds are capped at 1,000 USDT per sleeve
(`modes.live.max_seed_usdt`). Binance spot VIP0 is maker = taker = 0.100% (researcher-verified from
the fee schedule), so `LIMIT_MAKER` saves **zero** fee and recovers 0.001 bps of spread in exchange
for real fill risk — and VIP1 needs \$1M/30d volume, which at `max_turnover_pct_per_day: 0.50` on a
\$1,000 seed is ~\$15k/month, **67× short**. Earn will never leave VIP0.

**The only cost levers that exist are the BNB discount and the number of orders**, both of which are
already bounded by `risk.max_orders_per_day` and `risk.max_fee_pct_per_month` and already measured by
the `tca` skill. The runtime fee must be *read* (`sapi/v1/asset/tradeFee`, needs the reconcile
credential), never assumed.

---

## 2. What does not work, and why

Recorded so a future run does not rediscover each false positive. Everything here was **tested**, not
dismissed.

| Claim | The measurement that kills it |
|---|---|
| **OI deleveraging as a buy signal** | Worked, then died — worse than never working. `OI 24h < −5%`: H1 2023-09→2025-03 gave **+1.83% / 67% hit rate** vs 55% base; H2 2025-03→2026-09 gave **−0.18% / 46.5%** vs 51.4% base. Linear β goes t = −3.66 → **−0.04**. A full-sample backtest shows +1.16% and ships a corpse. Only the **drawdown** leg survived both halves. |
| **"High funding = overheated, sell"** | My own table (§1.2): the ≥40% funding bucket has the *second-highest* median 7d return (+1.41%). Backwards. |
| **Taker imbalance / CVD as alpha** | Two independent tests, both null. NW(18) of forward 72h return on standardised taker ratio over 6,523 4h bars: **t = +0.11**, and t = +0.07 after controls. The 8h quintile sort *looks* monotone (+0.20%→+0.58%) — that is how people get fooled: contemporaneous correlation with the same bar's return is **0.414**, so the sort re-sorts on past returns. CVD is a chart of where price has been. |
| **Order-book imbalance at 4h+** | t = −1.45 / −1.47 / −0.04 at 4h/24h/72h, sign even backwards from folklore. Cont–Kukanov–Stoikov put order-flow imbalance's horizon in **tens of seconds**; it is a market-making signal with no business in a 4h system. |
| **CME gap fill** | The strongest folklore in crypto, and it collapses against the right control. 475 weekends, gaps >0.5% filled within 7 days **68.4%** of the time — but random midweek 25-hour moves of the same size retraced **61.9%** of the time. The entire phenomenon is 6.5pp of ordinary mean reversion in a costume. CME's own data is IP-blocked from this host anyway. |
| **The 8h funding settlement clock** | 61,658 hourly bars: settlement hours +0.87 bps vs +0.51 bps otherwise, **t = 0.44**. No clock effect at all. |
| **The weekend effect** | Weekday +0.74 bps/h vs weekend +0.50 bps/h over 79,658 bars — no tradable asymmetry. Volume ratio *is* lower (0.65×) and decaying fast (0.98 in 2017 → 0.52 in 2024-26). And the common claim that weekend books are thin is **false**: weekend/weekday resting depth ratio is **1.021**. Quiet, not thin. |
| **The halving cycle** | n = 2 observable halvings and they disagree at every horizon: +365d was **+562%** in 2020 and **+31%** in 2024; −90d was +19.4% vs −36.0%. The "days since halving" bucket table looks compelling and is an artefact — the bins are two contiguous runs, so effective n is 2, not 180. Verified current position: mempool tip height **968,285**, so 81,715 blocks (~567 days) to the 1,050,000 halving. |
| **Cross-sectional momentum BTC vs ETH** | Sharpe **1.04 / 0.85 / 0.55** for 20 / 60 / 120-day lookbacks, and every variant has a worse max drawdown (−77% to −89%) than simply holding BTC. Parameter instability of that size across a routine choice is the definition of an artefact. |
| **Cross-exchange / Coinbase premium as a 72h signal** | Mean spread −5.3 bps, sd 6.0, p95\|·\| 15.5 bps — **inside a single taker fee**, and most of it is the USDT basis (§1.6) rather than a dislocation. Nobody who looked completed a forward-return test; do not let "the data source is verified" become "the signal is verified". |
| **Execution-timing optimisation** | §1.10. Not a false signal — a false priority, and it would have consumed real effort. |
| **Exploit count as a risk input** | The DefiLlama hacks set (verified free, 1,283 events) has recent entries of \$4.9M, \$4.4M, \$35k and **\$16k**. Counting events treats a \$16k rug as a bridge failure. Only *size relative to the contagion surface* can matter, and the threshold must be calibrated before anything is wired — otherwise every exploit burns a validator slot under the existing fast-path rule. |

### Data that simply is not available free — verified by failure, not assumed

| Wanted | Status |
|---|---|
| **US spot ETF flows** | **None free.** farside.co.uk → **403** (verified by me); DefiLlama `/etfs` → 404; CoinGlass and SoSoValue key-gated; Yahoo `quoteSummary` → "Invalid Crumb". Painful, because the literature puts flows at ~21% of daily return variance. Do not let a model assert a flow number it cannot source. |
| **Historical liquidations** | **None free.** `fapi/v1/allForceOrders` → 404; `forceOrders` → 401, account-scoped; `data.binance.vision/.../liquidationSnapshot/` → **404 verified by me**; CoinGlass now paid. The public `wss://fstream.binance.com/ws/!forceOrder@arr` stream is free but forward-only and unbacktestable. Cascades must be **inferred** from OI + price. |
| **Token unlocks / emissions** | `api.llama.fi/emissions` → **402 verified by me**. A CDN workaround exists (`defillama-datasets.llama.fi/emissions/<protocol>`) but BTC has no unlock schedule and ETH's is not a cliff — **irrelevant to this universe**. |
| **Coin-days-destroyed, LTH supply** | Nowhere free. Glassnode / CryptoQuant only. Self-indexing a full UTXO set is out of proportion to a weak weeks-horizon input. **Explicitly declined**, recorded so nobody writes a skill that asks a model to estimate one. |
| **Real-time SOPR / NUPL** | bitcoin-data.com withholds the last **7 days** behind a subscription. A 7-day-old profitability reading cannot inform a 4h or even a 1d decision. **Bar it from the decide feature set** rather than letting a week-old number look current. |
| **Bridge volumes, Etherscan V1, beaconcha.in staking queue** | 402, deprecated, and 401-needs-signup respectively. |
| **Exchange-balance history (Blockscout)** | `coin-balance-history-by-day` returns exactly **10 days**. There is no free long history, so on-chain flow features **cannot be backtested** and must ship observe-only, accruing their own record. A "backtest result" on 10 days is noise with a number attached. |

### Loader traps that will silently corrupt a backtest

Three of these were found by researchers; the first I found myself in this session and it is not in
any research report.

1. **The metrics archive is not a uniform grid and is not sorted.** I parsed
   `BTCUSDT-metrics-2026-09-20.csv` (288 rows): it spans 00:30 → 23:55 but the inter-row gap
   histogram is `{5: 76, 10: 48, 15: 42, 20: 37, 25: 19, 30: 20, …, 185: 1}` **and includes three
   negative gaps of ≈ −1,400 minutes** — the rows are out of order. Any loader that assumes 5-minute
   spacing or monotone time will produce garbage. **Sort by `create_time`, deduplicate, and resample
   — never index by position.**
2. **Spot klines in the bulk archive switched from milliseconds to microseconds at exactly 2025-01**,
   while futures klines stayed in milliseconds and the REST API returns milliseconds everywhere.
   Branch on **digit count**, never on a constant.
3. **The metrics archive contains OI values near zero** that produce `inf` on `pct_change`. Clip or
   drop.
4. **`markPrice` is an empty string on early `fundingRate` rows** (verified: 2019-09 rows).

### Availability corrections to the research reports

| Report said | Verified truth (this session) |
|---|---|
| metrics archive from 2021-01 / 2021-06 | **2020-09-01 returns HTTP 200** (12,191 B). Five full years. |
| monthly funding archive from 2019-09 | **2019-09 and 2019-10 are 404**; 2020-01 is the first 200. Use the REST endpoint, which reaches 2019-09-10. |
| metrics archive is "5-minute resolution, 289 rows/day" | 288 rows/day, nominal 5-minute but **irregular and unsorted** (trap 1). |
| bookDepth ~34,500 rows/day | 28,560 rows for 2023-01-01. |

---

## 3. ML methods: what the sample size actually permits

This section exists because it is the reason most crypto-ML proposals should be refused, and the
arithmetic is not obvious.

### Justified

* **HAR-RV for volatility.** Log realised variance regressed on its daily/weekly/monthly means, OLS
  by `numpy.linalg.lstsq`, expanding refit. Measured OOS R² over 1,322 OOS days: **HAR(d,w,m) 0.116**
  · monthly-only 0.086 · **EWMA(0.94) 0.094** · **random walk −0.329**. The random-walk number is the
  important one: *using yesterday's vol as today's forecast is worse than using the unconditional
  mean*, and a surprising number of "ATR-based" sizing schemes do exactly that.
* **Linear regression with Newey-West standard errors.** Overlapping forward windows make naive
  t-stats liars; NW with lag ≈ the overlap is the minimum honest correction. Everything in §1 uses it.
* **Quantile/bucket sorts reported with the base rate next to them.** Every table in §1 carries its
  baseline row, because a monotone-looking sort without one is how the taker-imbalance false positive
  survives.
* **Volatility targeting.** Sharpe-neutral by construction and it must be sold that way: buy-and-hold
  BTC 2017-2026 gives CAGR 38.9% / vol 66.9% / **Sharpe 0.83** / MaxDD −83.2%; vol-target-40% gives
  28.8% / 42.5% / **Sharpe 0.81** / MaxDD −62.4%. Sharpe **unchanged**; vol down 24 points, drawdown
  up 21 points. Selling vol targeting as an alpha improvement is the most common honest-looking lie
  in this space.
* **Trend filter × vol target — and the evidence is the plateau, not the peak.** MA200 *alone* scores
  Sharpe **0.76, below buy-and-hold**. Vol target alone 0.81. The product scores 0.82–1.38 across
  *every* MA from 50 to 250, with months worse than −10% falling from 23 to 0–6. **The stability
  across the whole parameter range is the finding; the MA50 peak at 1.38 is the trap.** The
  defensible choice is the middle of the plateau (MA125, Sharpe 1.17, MaxDD −28.6%).
* **Constant fractional scaling beats a drawdown ladder.** Base MA100×volTarget: CAGR 34.7%,
  Sharpe 1.14, MaxDD −34.0%, 3 months below −10%. **×0.5 constant:** CAGR 17.4%, Sharpe **1.14
  (identical)**, MaxDD −18.3%, **zero** months below −10%. A drawdown ladder (1.0/0.5/0.25×): Sharpe
  **1.00 (worse)**, MaxDD −23.8%. The ladder is a forced momentum trade on your own equity curve; it
  gives up 0.14 of Sharpe and still drew down deeper than the flat scalar.

### Not justified at this sample size

* **Overlapping labels destroy the effective sample.** Triple-barrier labels on BTC 4h
  (pt = 2σ, sl = 1σ, 30-bar limit) give **19,929 rows** with average uniqueness **0.154** →
  **≈ 3,067 effective independent samples**, about **613 per fold** in a purged 5-fold CV. *Any
  proposal that quotes a row count instead of an effective-N is proposing a failure.*
* **Meta-labelling — run honestly and it made things worse.** Primary = MA100 × volTarget long days;
  6 standard features; ridge; expanding purged walk-forward with a 5-day embargo; 851 OOS days.
  **OOS accuracy 0.496 against a 0.503 base rate** — literally worse than always saying yes — and as
  a filter it cut Sharpe 1.16 → 0.80 and CAGR 53.5% → 23.1%. The constraint is effective sample size,
  not algorithm choice, so no new model fixes it.
* **HMM / changepoint regime detection.** A 2-state Gaussian HMM fitted **on the full sample**
  (deliberate look-ahead, i.e. the most flattering possible test) scored Sharpe **0.78 — below** the
  no-look-ahead MA200×volTarget at 0.87 and far below MA100×volTarget at 1.14. The trend/vol quadrant
  does the same job with two comparisons.
* **Deep learning on OHLCV.** ~3,000 effective samples and maybe three genuine regimes in nine years.
  Parameter count over information.
* **Multiple testing is not optional.** On 9.1 years the sd of an annual Sharpe estimate is
  1/√T ≈ 0.33, so the *expected best* in-sample Sharpe from **N zero-skill trials** is
  **N=10 → 0.86 · N=50 → 1.05 · N=200 → 1.19 · N=1000 → 1.33**. The MA50 Sharpe of 1.38 is barely
  outside search noise. A system that auto-proposes changes **is** a search process and must count its
  own trials. `review.change_gates.walk_forward_min_out_sample_delta: 0.0` is a coin-flip hurdle and
  should be replaced by a deflated one.
* **Live testing cannot prove edge.** Distinguishing Sharpe 1.14 from 0.83 at 80% power needs
  **219 years**; even 2.00 vs 0.83 needs 15.4. `modes.live.min_test_days: 90` proves the *plumbing*
  works and **nothing** about edge. The system should say so out loud rather than letting a good
  quarter be read as validation.
* **Survivorship-clean-looking alt universes.** Any widening beyond BTC/ETH that picks today's liquid
  listings selects on coins that *survived* — the delisted ones are not in the klines API at all.

---

## 4. The skill set

Five skills. The test for each was: *does it change a decision the system makes, from free verified
data, at our cadence, in a way that survives a backtest and a replay?* Everything that only restated
what `market-state` already computes was cut.

All five share a shape: **Python computes numbers, the model reasons over numbers, and the
deterministic gate reads flags.** None returns a direction. Three can only ever *tighten* risk, which
means a stuck input can never manufacture a buy.

### 4.1 `vol-surface` — what volatility will be, and how large the position may be

**Purpose.** Produce `sigma_hat` (forward annualised volatility, per pair) and the position scalar
derived from it, replacing the trailing-realised-vol denominator that `sleeve_a.vol` uses today.

**Why it earns its place.** This is the single highest-confidence change available and I replicated it
myself (§1.1): DVOL beats trailing realised vol on forward 7d realised vol, R² 0.268 vs 0.167, and in
a joint regression DVOL holds **t = +7.95** while trailing realised collapses to **t = −0.33**. Both
halves significant. It changes a live number: `sleeve_a.vol.lookback_days: 20` is exactly the
backward-looking estimator DVOL subsumes.

**Computed by code.** From Deribit `get_volatility_index_data` (hourly/daily, free, keyless, verified,
BTC from 2021-03-24, ETH from 2023-12-27): `dvol_last`, `dvol_pctile_2y`, `dvol_chg_5d`. From local
4h candles: `rv_d` (sum of squared 4h log returns per UTC day), HAR(d,w,m) fitted by `numpy.lstsq` on
an expanding window with a 30-day refit, and `har_oos_r2_250d` as a self-health number. Combined:
**`sigma_hat = a + b·DVOL` refit on an expanding window** (full-sample a = 6.28, b = 0.707 — the
builder's sanity target), `vrp = dvol − trailing_30d_realised` (full-sample mean +8.09 vol points),
`vol_target_scalar = clip(target_annual / sigma_hat, 0, 1)`. From
`get_book_summary_by_currency?currency=BTC&kind=option` (verified, **970 instruments in one request**,
carries `mark_iv`, `underlying_price`, `open_interest`): 25-delta call/put IV by local Black-Scholes
delta, `rr25`, `butterfly`, and `deribit_option_oi_total` as the venue-migration canary.

**Model reasons about.** Whether the vol regime is one the strategy should be in, what the scalar
implies for the proposal, and whether `sigma_hat` disagrees with what the brief and the dossier say.
It never estimates a volatility and never overrides the scalar — it may only argue for *less*.

**Outputs.** `knowledge/state/volsurface.json` — per pair `sigma_hat`, `source: dvol|har`,
`dvol_last`, `dvol_pctile_2y`, `vrp`, `vol_target_scalar`, `har_oos_r2_250d`, `as_of`, `staleness_min`,
and `rr25`/`butterfly` **labelled `observe_only: true`** until the skew series has six months of
self-collected history. A short paragraph appended to the market-state narrative.

**Hard stops.** Refuses to emit `sigma_hat` when `har_oos_r2_250d < 0.05` (the model is broken, not the
market) or when the DVOL series is staler than one bar — it falls back to HAR and **says which one it
used**. Uses the **fitted** mapping, never raw DVOL (§1.1: raw over-states vol 13–29%). Never emits a
direction. Never widens a scalar above 1.0.

**Bound to** `validate`; its JSON is read by the `decide` stage and by `research_run`.

**Tier.** Body tier 1 (gated). `scripts/**` tier 2 — and because a `skill_new` containing `scripts/**`
is **always held for a human** (code invariant), shipping this needs Shourya's approval plus a tier-2
`config/earn.yaml: skills.bindings` edit.

**Tests and evals.** Golden-value test pinning `sigma_hat` for a frozen DVOL+candle fixture. A
regression test asserting DVOL beats trailing realised on the frozen panel (R² 0.268 vs 0.167 ± tol).
A refusal test: `har_oos_r2 = 0.03` ⇒ no forecast emitted. An ETH test: DVOL absent before 2023-12-27
⇒ `source == "har"`. A replay against `evals/signal_replay.py`. **A negative test that raw DVOL is
*not* used** — assert the emitted `sigma_hat` at DVOL 60 is ≈48.7, not 60.

**Builder: B1.**

---

### 4.2 `leverage-state` — how crowded the leverage is, and how much to take off

**Purpose.** Turn the public leverage record into a **forward drawdown forecast** and a
**tightening-only exposure multiplier**. Explicitly not a direction.

**Why it earns its place.** This is the part of crypto that has no equity analogue, it is free, and it
is the only family in this study whose *tail* prediction survived split-half testing in every
researcher's hands and mine. My own replication (§1.2): P(7d drawdown < −8%) rises **17.0% → 41.4%**
monotonically across funding buckets while the return column stays positive throughout, and the
ratio to baseline **strengthened** across halves (1.30 → 1.46). Add OI crowding (§1.3, forward 72h
drawdown −2.60% vs −2.26%, p10 −7.11% vs −5.85%, stable both halves) and the dip-into-crowded-longs
conditioner (8.3% vs **42.9%** tail risk on the same −5% day). It answers the owner's instruction
directly: *see things like spot price or loss sell to stop loss* — this is the numeric layer that
decides how much room a stop needs and whether a dip is likely to overshoot.

**Computed by code.** Funding from `fapi/v1/fundingRate` (verified full history to 2019-09-10, 7,711
prints) and live `premiumIndex`: `fr_8h`, `funding_ann_3d`, `funding_pctile_1y`, `fr_sign_run_length`,
`fr_capped_flag` (from `fundingInfo`). Open interest from the bulk metrics archive (verified
2020-09-01+) and live `fapi/v1/openInterest`: `oi_chg_24h`, `oi_z_180`, `oi_notional_to_adv`, and the
**four-quadrant label** `sign(price_ret_24h) × sign(oi_chg_24h)`. Basis from paired spot/perp klines
and `premiumIndex`: `basis_bps`, `basis_z_180`, `backwardation_flag` (z-based, never raw — mean basis
is **−1.38 bps**, §1.5). Positioning from the archive: `tlsr`, `tlsr_z`, and the top-trader-vs-global
**spread** (the two cohorts were verified positioned oppositely at the same timestamp, which is itself
the feature). Cascade **proxy** from OI + price, never from liquidation data, which does not exist free.
Then the outputs that matter: `crowding_state ∈ {clean, mixed, crowded}`, `p_drawdown_7d` read off the
**empirical bucket table recomputed on a rolling window**, and `exposure_multiplier ∈ (0, 1]`.

**Model reasons about.** What the crowding state means for this specific proposal, whether the
evidence pack's funding/OI/basis agree or conflict, and what would invalidate the read. It is handed
`P(7d dd < −8%) = 41%` rather than being asked to judge whether funding is "high".

**Outputs.** `knowledge/state/leverage.json` with every feature, its `as_of` and staleness; a
`derisk` flag written **only** through `ops.lib.flags`; and an evidence-pack block for the validator.

**Hard stops.** `exposure_multiplier` is **clamped to ≤ 1.0 and can only ever reduce** — it may shrink
a target weight and can never raise one, so a stuck or absent input degrades to "no change", never to
"add". Emits **no direction** from funding, ever (§2: the ≥40% bucket has the second-highest median
7d return). Refuses to act on any liquidation-derived feature in a backtest, because no free
historical liquidation data exists. Thresholds are **rolling percentiles**, never the fixed numbers in
this document.

**Bound to** `validate` and `scanner` (its flags feed the existing detectors; the deterministic
`funding` detector at `abs_8h ≥ 0.0010` already sits exactly at the measured extreme cut — ~13
events/year — so it fires rarely enough to escalate and often enough to matter).

**Tier.** Body tier 1; `scripts/**` tier 2, human-approved. Needs the archive backfill (§5) before any
of it is backtestable.

**Tests and evals.** Golden values on a frozen archive slice. A **monotonicity test** asserting
`p_drawdown_7d` is non-decreasing across funding buckets on the frozen panel. A **clamp test**:
multiplier > 1.0 is impossible by construction. A **null test** asserting the skill emits no
direction field at all. Loader tests for all four traps in §2 — out-of-order rows, µs timestamps,
near-zero OI producing `inf`, empty `markPrice`. A split-half test that re-derives the ratio
(1.30 / 1.46) and fails if the top bucket comes within 3pp of baseline.

**Builder: B2.**

---

### 4.3 `venue-guard` — can this order be filled, and in what unit

**Purpose.** A deterministic pre-trade hazard skill answering two questions the rest of the system
assumes: *is the venue accepting orders in our symbols*, and *is our quote currency still worth a
dollar*.

**Why it earns its place.** Every Earn position is denominated in USDT and nothing in the system
currently checks the peg from a price feed — `reg-watch` infers depeg from **news RSS**, which is
strictly slower and less precise than quoting the pair we trade on the venue we trade it on. And the
USDT-basis correction (§1.6) rescues every cross-venue number in the system from silently measuring
Tether. It is a hazard detector, so it does not decay; its value is entirely in the tail, which is
exactly the risk a long-only spot book cannot hedge.

**Computed by code.** `symbol_status[pair]` and the full filter set from
`api/v3/exchangeInfo` (verified: BTCUSDT/ETHUSDT `TRADING`, tickSize 0.01, stepSize 0.00001,
`NOTIONAL.minNotional 5.0` — note `risk.min_notional_usdt: 25` is the stricter binding one).
`usdt_usd_mid` from Coinbase `products/USDT-USD/ticker` (verified 0.99978) and Kraken as the second
source; `usdc_usdt_close` and `fdusd_usdt_close` from Binance klines (verified);
`peg_dev_bps`, `depth_at_peg`, and `depeg_flag` requiring **3 consecutive hourly closes >50 bps off
par** (§1.6: the worst single low is 0.7600 with a 0.9995 close — wick, not depeg) **and** two
independent sources agreeing. `usdt_basis_bps`, and `dispersion_bps_corrected` computed **only after**
dividing the Binance price by the live USDT/USD rate. `delist_hits_24h` from the CMS catalog
(verified, catalogId 161, 436 articles, 2–14 day lead) plus `sapi/v1/system/status`.

**Model reasons about.** Only the narrative — what a depeg's *sign* means (USDT at a premium during
someone else's depeg is inbound contagion; USDT at a discount is our own NAV failing, and is far more
serious). It never decides whether to block.

**Outputs.** Blackout flags through `ops.lib.flags` (`depeg`, `symbol_halted`, `delist_notice`) and
`knowledge/state/venue.json`.

**Hard stops.** Only `exchangeInfo` may **block** — the undocumented `bapi` announcements endpoint is
best-effort with a canary and may only *warn*. It is barred from producing any alpha score. The
absence of an announcement is never an all-clear. It can only block, never permit.

**Bound to** `scanner` (per cycle) and `research.flags`.

**Tier.** Body tier 1; `scripts/**` tier 2. Writing to `knowledge/flags.json` is tier 2 and must go
through `ops.lib.flags`, which is the existing sanctioned path.

**Tests and evals.** **The 2024-01-03 wick fixture must not fire** (low 0.7600, close 0.9995) and the
**March 2023 four-close sequence must fire** (0.9592 → 0.9961). A test that any cross-venue figure is
USDT-corrected. A canary test that a `bapi` 403 degrades to warn and never blocks. A test that a
non-`TRADING` status blocks entries.

**Builder: B3.**

---

### 4.4 `event-blackout` — when not to be trading

**Purpose.** Own the macro-event window deterministically, and argue its width from measurement rather
than habit.

**Why it earns its place.** It produces a concrete, measured correction to a shipped config value.
`risk.blackout.window_minutes: 60` is symmetric and covers only the 2.26× peak hour, while the
measured elevation runs **T−7h (1.51×) through T+8h (1.19×)** and returns to baseline at T+9h
(§1.7). Meanwhile direction is a coin flip (T→T+24h mean −0.27%, sd 3.18%). Volatility 1.81× with
zero expected return is a pure cost to enter into. And the real failure mode of a hand-maintained
calendar is that it silently runs out — which nothing currently detects.

**Computed by code.** `in_macro_blackout(now, event_time, pre_min, post_min)` from
`config/macro_calendar.yaml`; `hours_to_next_macro_event`; `calendar_stale_days` =
(today − furthest future event). FOMC dates auto-verified against
`federalreserve.gov/monetarypolicy/fomccalendars.htm` (**verified by me: 47 statement dates parse via
`monetary(\d{8})a\.htm`**). Past CPI entries audited after the fact against
`api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0` (**verified keyless 200**, `latest:true` flips
at release). And the event study itself: `vol_multiplier_20ev` = the rolling |4h| return ratio over
the last 20 events, so the window's width is re-argued from data, not frozen.

**Model reasons about.** Nothing about the print. It reads the multiplier and the window and may
recommend a width change through the normal change-proposal path. It is explicitly forbidden from
predicting a print's direction or its surprise.

**Outputs.** A blackout flag via `ops.lib.flags`, `knowledge/state/macro.json`, and a change proposal
when `vol_multiplier_20ev` drifts materially from the shipped window's justification.

**Hard stops.** Refuses to predict direction. Raises `calendar_stale` when the furthest future event
is under **45 days** out. Never silently extends a window — a width change goes through
`changes/*.json` like any other.

**Bound to** `research.flags` and `daily`.

**Tier.** Body tier 1; `scripts/**` tier 2. `config/macro_calendar.yaml` is tier 2 and stays
human-maintained — the skill audits it, it does not write it.

**Tests and evals.** An event-study regression test reproducing the hour-by-hour profile on a frozen
fixture (peak at T−1h). A staleness test. A test that the FOMC regex still yields ≥40 dates, and a
degradation test for when federalreserve.gov is unreachable (keep last, flag, never fail open).

**Builder: B3.**

---

### 4.5 `edge-audit` — the skill that keeps the other four honest

**Purpose.** Own effective sample size, purged cross-validation, the deflated-Sharpe hurdle, and the
quarterly re-test of every shipped feature.

**Why it earns its place.** One measured fact justifies it on its own: **OI deleveraging as a buy
signal worked and then died** — +1.83% with a 67% hit rate in 2023-25, −0.18% with a 46.5% hit rate in
2025-26, linear t from −3.66 to −0.04. A naive full-sample backtest shows +1.16% and ships a corpse.
Nothing in the repo today would have caught that. Meanwhile `review.change_gates` asks for
`walk_forward_min_out_sample_delta: 0.0` — a **coin-flip hurdle** — while the expected best Sharpe
from 200 zero-skill trials on this sample is **1.19**. And the meta-labelling failure (0.496 vs a
0.503 base rate) is precisely what an effective-N check refuses in advance. `strategy-lab` runs the
protocol; this skill supplies the statistics that decide whether the protocol was passed.

**Computed by code.** Triple-barrier labels; per-label concurrency and uniqueness
(**measured: 19,929 rows → average uniqueness 0.154 → EFFECTIVE_N ≈ 3,067**); the repo's only CV
splitter, purged by max label span and embargoed; `expected_max_SR(N, T) =
((1−γ)√(2 ln N) + γ√(2 ln(N e²))) / √T_years` with γ = 0.5772 and a **persistent trial counter N**
stored in the change record so it cannot be reset by forgetting; `years_to_detect(SR_a, SR_b, power)`
(measured: 1.14 vs 0.83 needs **219 years**); and the quarterly decay panel — a rolling 12-month NW
t-stat and ratio-to-baseline for **every** feature `vol-surface` and `leverage-state` ship.

**Model reasons about.** Whether a candidate change clears `baseline + expected_max_SR(N,T)` rather
than merely beating the baseline, and whether a decaying feature should be retired. It writes the
verdict; it does not compute the statistics.

**Outputs.** `reports/edge-audit-<quarter>.md`; an `EFFECTIVE_N` and a deflated hurdle attached to
every change proposal; a `feature_retire` recommendation when a feature fails |t| > 1.5 for two
consecutive quarters; and the `years_to_detect` figure attached to every post-mortem so a good quarter
is never mistaken for validation.

**Hard stops.** Refuses to report any CV score computed without purging and an embargo. Refuses any
proposal quoting a **row count** instead of an effective-N. Never lowers the hurdle; N only grows.

**Bound to** `review` and `daily`.

**Tier.** Body tier 1; `scripts/**` tier 2.

**Tests and evals.** A uniqueness test reproducing 0.154 on the frozen 4h panel. A leakage test
asserting the splitter removes `[test_start − max_span, test_end + embargo]`. A hurdle test
(N=200, T=9.1 ⇒ ≈1.19). A refusal test for an unpurged score. A **regression test that replays the
2023-25 OI signal and fires the retirement rule at the H1/H2 boundary** — i.e. proof that this skill
would have caught the corpse.

**Builder: B1.**

---

### Skill-to-run map

| Skill | scanner (5 min) | research.flags | validate | decide | daily | review |
|---|---|---|---|---|---|---|
| `vol-surface` | — | — | **loads** | reads JSON | — | — |
| `leverage-state` | flags | — | **loads** | reads JSON | — | — |
| `venue-guard` | **loads** | **loads** | — | — | — | — |
| `event-blackout` | — | **loads** | — | — | **loads** | — |
| `edge-audit` | — | — | — | — | **loads** | **loads** |

This matches the owner's requested split: **deterministic Python monitors continuously** (the existing
detector layer plus `venue-guard` and `leverage-state`'s flags), **the cheap local tier triages**,
**Claude consolidates on a schedule** (validate / decide / daily / review), and **nothing reaches an
order without the 17-check gate**.

---

## 5. Shared feature modules — `runs/features/`

`runs/**` is **tier 2, human-only**. These are written by a human, not by a Claude run. Every skill
script imports from here; no skill computes a feature twice.

| Module | What it owns | Owner |
|---|---|---|
| `runs/features/__init__.py` | The **feature registry**: every key, its source URL, whether a key is needed, its rate limit, its true `as_of`, and its `max_lag`. One audited place, so a paywalled or stale metric can never quietly become a model estimate. Also the NOT-AVAILABLE-FREE list (ETF flows, liquidations, CDD/LTH, bridge volumes, real-time SOPR) so a future run does not rediscover each paywall. | **B1** |
| `runs/features/deribit.py` | DVOL paging (backwards, ≤1000 points/request, browser UA required); option chain → 25Δ skew by local Black-Scholes delta from `mark_iv` + `underlying_price`; `deribit_option_oi_total` as the venue canary. | **B1** |
| `runs/features/volatility.py` | Realised variance from 4h bars; HAR(d,w,m) by `numpy.lstsq` with expanding refit; rolling 250d OOS R²; the fitted `sigma_hat = a + b·DVOL` mapping; VRP; `vol_target_scalar`. | **B1** |
| `runs/features/sampling.py` | Triple-barrier labelling, concurrency/uniqueness, EFFECTIVE_N, the purged+embargoed CV splitter, `expected_max_SR`, `years_to_detect`. numpy-only — the WSL venv has **no scikit-learn and no statsmodels** (verified: numpy 2.5.3, pandas 3.0.6), which is also the right call for auditability. | **B1** |
| `runs/features/binance_archive.py` | The `data.binance.vision` downloader for `metrics` (2020-09-01+), `bookDepth` (2023-01-01+) and klines, with all four loader traps handled: **sort + dedupe out-of-order rows**, **branch timestamp parsing on digit count** (spot klines switched ms→µs at 2025-01, futures did not), clip near-zero OI before `pct_change`, tolerate empty `markPrice`. Threaded, resumable, idempotent. This is the prerequisite that makes anything in §1.3–1.5 backtestable. | **B2** |
| `runs/features/derivatives.py` | Funding (REST full history + live), OI z/Δ and the four-quadrant label, perp–spot basis (z-scored), top-trader vs global positioning spread, the cascade proxy, `crowding_state`, and the empirical forward-drawdown bucket table recomputed on a rolling window. | **B2** |
| `runs/features/venue.py` | `exchangeInfo` status and filters; stablecoin peg with **persistence**; the **USDT-basis correction** applied to every cross-venue figure; the CMS announcements client with its canary. | **B3** |
| `runs/features/macro_calendar.py` | FOMC date scraping (`monetary(\d{8})a\.htm`), CPI post-hoc audit via `api.bls.gov` v1, `in_macro_blackout` with **asymmetric** pre/post minutes, `calendar_stale_days`, and the rolling event-study multiplier. | **B3** |

### Two non-skill changes that should ship with them

1. **A forward-only liquidation recorder**, inside the existing `runs/ingest.py`, on
   `wss://fstream.binance.com/ws/!forceOrder@arr` (free, keyless, no history). It is **not a skill**
   and must not feed a decision for at least twelve months, because it cannot be backtested at any
   free price. It is worth starting *now* precisely because every day not collected is permanently
   lost. Needs a websocket library installed (neither `websockets` nor `websocket-client` is present).
2. **Point-in-time archiving** of `exchangeInfo`, funding, OI and the metrics rows on every ingest, so
   the 30-day-capped and vendor-restated series become backtestable a year from now instead of never.

---

## 6. What was rejected, and why

Ideas that were proposed by a researcher and deliberately **not** built.

| Rejected | Reason |
|---|---|
| **`entry-timing` / `cost-truth` / any execution-timing skill** | §1.10, measured twice. Slippage is 0.001 bps (BTC) and 0.019 bps (ETH) at every size we trade, against a 10 bps fee, and maker = taker at VIP0 which we will never leave. Optimising under 1% of the cost line. The cost truth belongs as a **reference page inside the existing `tca` skill**, not as a new skill. |
| **`forceorder-collector` as a skill** | Right instinct, wrong shape. It produces nothing decision-usable for twelve months and cannot be backtested at all. Ships as an **ingest recorder** (§5), not a skill. |
| **On-chain exchange flows (`exchange-flows`, `whale deposits`)** | Blockscout balance history is **10 days** and mempool.space gives cumulative sums we must accumulate ourselves — so these **cannot be backtested** and fail the project's own bar. The strongest published result is that BTC net inflows *lack* return predictability except at 4h. Observe-only at best; not a skill yet. |
| **`onchain-valuation` (MVRV reconstruction)** | Genuinely clever — `bitcoin-data.com/v1/realized-cap/last` is **not** delayed (verified: `{"d":"2026-09-22","realizedCap":1.076e12}`) while MVRV/SOPR/NUPL are, so it routes around the Glassnode paywall. But it is a weeks-to-months tilt for a system that decides at 4h/1d, every published threshold is known-overfit across cycles, and **no forward-return evidence at our horizon was produced**. It does not change a decision. Recorded as a dossier context number, not a skill. |
| **`miner-state` (hash ribbons)** | Graded *folklore* by its own researcher. A handful of non-independent events each rationalised after the fact, and post-2024 miners hedge rather than force-sell. |
| **`eth-network-state` (EIP-1559 burn)** | `eth_feeHistory` reaches back ~1,024 blocks, so history needs a per-block backfill — and the "ultrasound money" framing decayed hard after EIP-4844 moved L2 data to blobs. A demand gauge, not a supply story, with no measured link to our decisions. |
| **`unlock-calendar`** | `api.llama.fi/emissions` is **402** (verified) and **BTC has no unlock schedule while ETH's is not a cliff**. Irrelevant to this universe. Would be one of the more reliable effects if the universe ever widened. |
| **`session-flow-proxy` (US-hours vs Asia-hours spread as an ETF proxy)** | Free and backtestable, and honestly labelled by its researcher — but it is a proxy for a number we cannot see, with no measured relationship to the number it proxies. It would be a feature in search of a validation. |
| **`peg-watch` as a standalone skill** | Merged into `venue-guard`. Depeg and symbol-halt are the same question — *can this order be filled, and in what unit* — and they write the same flags. |
| **`derivatives-state` / `positioning` / `carry-monitor` as separate skills** | All three are the same feature set. Merged into `leverage-state`. Three thin skills reading one JSON is worse than one skill that owns it. |
| **`vol-forecast` and `vol-surface` as two skills** | Merged. "What will volatility be" has one answer, with DVOL primary and HAR as the fallback; splitting them guarantees two skills disagreeing about `sigma_hat`. |
| **`overfit-audit` + `sample-power` + `signal-decay-monitor` + `label-lab` + `regime-compare`** | Five thin statistical skills merged into **`edge-audit`**. They share one contract — *is this claim real at this sample size* — and splitting them means four of them get skipped. |
| **A Claude "risk reviewer" agent in front of the gate** | `strategies/riskgate.py` runs 17 deterministic checks in the Freqtrade callback path. A model asked to review risk before that either agrees (wasted tokens) or disagrees (and is overruled, because the gate is the invariant). **The gate is the risk reviewer.** |
| **A separate Claude "validator" persona** | `validate` already returns verdict, confidence, thesis, counter-evidence and an invalidation condition, and `decide` already consumes that block. More agents means more handoffs, not more checks. |
| **An LLM "market monitor"** | The monitor already exists and is **deterministic Python** (`runs/signals/features.py` + `detectors.py`, ten detectors, fast path). A model at the watching layer would be slower, non-deterministic across restarts, unbacktestable, and would need its own gate. The local tier's correct job is **triage**, one step later — and it should be tuned for **recall**, because a precision-tuned local screener is a local screener quietly deciding which real signals Claude never sees. |
| **New tradeable assets / cross-sectional strategies** | Universe stays BTC/ETH. Dominance, breadth and alt beta are admissible only as *inputs*, and cross-sectional momentum on N=2 is a coin flip with two outcomes (§2). |

---

## 7. Config findings, separate from the skills

These are one-line changes that the measurements above justify, and they are worth more than several
of the rejected skills. Each is tier 2 and human-only.

| Key | Today | Measured | Evidence |
|---|---|---|---|
| `risk.blackout.window_minutes` | `60`, symmetric | ~`pre: 360 / post: 480` | §1.7 — elevation runs T−7h to T+8h; 60 min covers only the peak. |
| `review.change_gates.walk_forward_min_out_sample_delta` | `0.0` | `baseline + expected_max_SR(N, T)` | §3 — expected best from 200 zero-skill trials is 1.19. 0.0 is a coin flip. |
| `sleeve_a.vol.lookback_days` | `20` (trailing realised) | `sigma_hat` from `vol-surface` | §1.1 — DVOL subsumes trailing realised (t +7.95 vs −0.33). |
| `risk.max_weight` | `{BTC: 0.40, default: 0.30}` | portfolio-level vol cap | §1.9 — n_eff = 1.12; per-asset caps do not constrain the risk that shows up. |
| `trading.defaults.entry_price` + `reprice_on_timeout` | passive bid, reprice machinery | reconsider | §1.10 — maker = taker at VIP0, so this harvests 0.001 bps at real fill risk. |
| `budgets.monthly_usd` vs per-task caps | `150` vs per-task sum ≈ `224` | reconcile | The caps are not self-consistent; the first to bite will be a per-task cap, not the global one. |

---

## 8. Build assignment

Disjoint files, no shared edits.

**B1 — volatility and statistical honesty**
`.claude/skills/vol-surface/**` · `.claude/skills/edge-audit/**` ·
`runs/features/__init__.py` · `runs/features/deribit.py` · `runs/features/volatility.py` ·
`runs/features/sampling.py`

**B2 — the leverage record and the archive that makes it backtestable**
`.claude/skills/leverage-state/**` · `runs/features/binance_archive.py` ·
`runs/features/derivatives.py`

**B3 — venue and calendar hazards**
`.claude/skills/venue-guard/**` · `.claude/skills/event-blackout/**` ·
`runs/features/venue.py` · `runs/features/macro_calendar.py`

B2 carries one skill but the heaviest single piece of work: the bulk-archive backfill is ~1,900 daily
files with four distinct loader traps, and **nothing in §1.3–1.5 is backtestable until it lands**.
Build it first.

---

## 9. Where the data actually is

A correction that would otherwise waste a builder's day. The Windows checkout at
`C:\Users\Shourya Salaria\OneDrive\Documents\momentum` has **no `data/` directory** and
`knowledge/earn.db` is empty. The populated copy is the **WSL** checkout:
`/home/shourya/earn-run/data/binance/*.feather` — verified present for BTC ETH BNB SOL XRP ADA DOGE
AVAX LINK DOT LTC TRX at 1h/4h/1d (BTC 1d: 3,324 rows, 2017-08-17 → 2026-09-22). The venv with numpy
2.5.3 / pandas 3.0.6 is `/home/shourya/earn-dev/.venv/bin/python`. Every study in this document was
run there. This is consistent with the README: the checkout **must** be on ext4 inside WSL, because
SQLite WAL locking and git worktrees corrupt on a `/mnt/c` drvfs mount.
