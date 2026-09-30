# Track 2 — the parameters we are not looking at

**Question (the owner, verbatim):** *"are we checking all params like recent buy volume or
volume and other params to know how well it will peak or drop"*

**Run:** 2026-09-30, workspace `im2`. Snapshot-only: no live database was opened, copied or
read. Data is the read-only feather store (for a column audit) and public Binance spot REST
(`/api/v3/exchangeInfo`, `/api/v3/klines`, `/api/v3/ticker/24hr`) — keyless, rate-limited.

**Answer in one line:** no, Earn is not looking at the buy side — and after measuring it on
468 coins across nine years, **it should not start**. The buy-side volume the owner named is a
0.99 correlate of total volume, so it carries no new information. The one genuinely new thing
in the data (the *share* of a bar that was buying) is statistically overwhelming in the 383
coins Earn is refused from trading and dead in the 31 it may trade. Meanwhile the single
feature that best orders "will this drop" is one Earn **already computes for every watchlist
name and wires to nothing**.

---

## PART 0 — the pre-registration, sealed before any number was computed

Sealed at `~/earn-scratch/track2/PREREG.md`, 66 lines, written before the first fetch.
Reproduced in full so the falsifiers can be checked against what was found.

| ID | Statement | Falsifier | Outcome |
|---|---|---|---|
| H2.1 | Cross-sectional rank IC of taker-buy ratio vs next-bar return ≥ 0.02 with Newey-West t ≥ 3.3 | \|IC\| < 0.02, or \|t\| < 3.3, or sign flips between sample halves | **REFUTED** in the tradeable universe; magnitude fails everywhere |
| H2.2 | Same at 1h and 4h | as H2.1 | **REFUTED** on magnitude (best \|IC\| 0.0193 vs bar 0.02) |
| H2.3 | Rising price + falling volume underperforms rising price + rising buy volume by > 0.30% over 5 days, \|t\| ≥ 3.3 | spread ≤ 0.30%, \|t\| < 3.3, **or the sign is inverted** | **REFUTED — the sign is inverted** |
| H2.4 | Volume acceleration / trade count / avg trade size clear H2.1's bar | as H2.1 | **REFUTED** in the tradeable universe |
| H2.5 | **The deciding test.** Any survivor keeps \|t\| ≥ 2.0 after controlling for log dollar volume, vol_60, log age, prior return (Fama-MacBeth, day fixed effects, NW errors) | \|t\| < 2.0 ⇒ the feature is a volume/vol proxy and is worth nothing | **REFUTED — nothing reaches \|t\| = 2.0 in the tradeable universe; the best is 1.08** |
| H2.6 | IC sign holds in ≥ 4 of 5 purged folds and the last fold is positive | fewer than 4 of 5, or a negative last fold | **REFUTED** for the buy-share features (2/5 and 3/5) |
| H2.7 | A decile book clears the deflated hurdle 2.32 net of 0.30% | Sharpe < 2.32 | **REFUTED — every book is negative net; best net Sharpe 0.035** |
| H2.8 | Declared untestable in advance (spread/depth have no history and the live DB is off-limits) | n/a | Code-read claim only, delivered below |

**Trials this study adds: 225 measured statistics.** The pre-registration set the
multiplicity bar for an estimated 45 trials (Bonferroni z = 3.28, rounded to 3.3). At 225 the
correct bar is **z = 3.72**. I am disclosing the overrun rather than quietly keeping the
easier bar. It changes no conclusion: nothing that failed at 3.3 would pass at 3.72, and
everything above 3.72 sits in the universe Earn is refused from trading.

**Cumulative selection trials: 8,144 + 225 = 8,369.** Deflated hurdle unchanged at ~2.32
(BTC-hold arithmetic Sharpe 0.83 + expected max Sharpe). Costs on at 0.30% per round trip in
every traded number. Sharpe is the repo's arithmetic definition, mean/std × √365.

---

## PART 1 — HALF ONE: the inventory. Every parameter Earn computes today.

### 1.1 The two tiers, and the number that matters

`runs/signals/features.py` computes features in two tiers.

**The cheap tier is 6 keys.** `CHEAP_KEYS` at line 61:

| # | Key | What it is |
|---|---|---|
| 1 | `close` | last daily close |
| 2 | `ret_24h` | last daily bar's open→close return, % |
| 3 | `vol_ann_20d` | annualised stdev of 20 daily log returns, % |
| 4 | `ma200_dist_pct` | distance from the 200-day MA, % |
| 5 | `dip_from_high_pct` | drawdown from the 30-day high, % |
| 6 | `news_count_24h` | news items mentioning the asset in 24h |

**The rich tier is 25 keys** (24 distinct values — `drawdown_pct` is an exact alias of
`dip_from_high_pct`; the module docstring says "24 keys", which is right about values and one
short about keys):

| Key | Source | Read by a detector? |
|---|---|---|
| `close` | 1h candle | yes — `breakout` |
| `ret_1h` | 1h candle | yes — `move`, `volume_spike` |
| `ret_4h` | 4h candle | yes — `move` |
| `ret_24h` | 1d candle | yes — `move` |
| `rsi_4h` | 4h closes, Wilder | yes — `rsi_extreme` |
| `atr_pct_4h` | 4h true range | **no** |
| `vol_z_1h` | z-score of 1h base volume, 72 bars | yes — `volume_spike` |
| `rvol_1h` | last 1h volume ÷ mean | **no** |
| `vol_ann_20d` | 20d daily vol | **no** (used only in `rank_watchlist` attention scoring) |
| `ma200_1d` | 200d SMA | **no** |
| `ma200_dist_pct` | distance to it | **no** (attention scoring only) |
| `ma_fast_1d` / `ma_slow_1d` / `_prev` ×2 | 50/200 SMA now and one bar back | `ma_cross` — **and `ma_cross` is `enabled: false`** |
| `range_high_20d` / `range_low_20d` | 20d high/low | yes — `breakout` |
| `high_30d` | 30d high | yes — `dip_from_high` (rich pairs only) |
| `dip_from_high_pct` | drawdown from it | yes — `dip_from_high` |
| `drawdown_pct` | alias of the above | **no** |
| `funding_8h` | `funding_current` table | yes — `funding` |
| `oi_delta_pct` | `open_interest` table, last 2 rows | **no** |
| `spread_bps` | `book_snapshots`, latest row | **no** |
| `news_count_24h` | `news_items` | **no** |
| `news_corroborated_24h` | `news_items` | yes — cited by `news_event` |

Plus 7 globals: `regime`, `regime_asof`, `last_decide_utc`, `regime_at_last_decide`,
`regime_flip`, `watchlist_size`, `rich_pairs`.

### 1.2 The coverage gap the owner is really asking about

`DEFAULT_RICH_PAIRS = 20` (line 68), and `signals.scanner` gives the rich tier to core
(BTC, ETH) + whatever the sleeves hold + the highest-ranked watchlist names up to the budget.
The watchlist is 107 names. So:

> **About 87 of 107 watchlist names only ever get 6 numbers.** No RSI, no ATR, no volume
> z-score, no funding, no spread, no moving-average cross. For those names the only detectors
> that can possibly fire are `move` (24h) and `dip_from_high`.

That is not a bug — it is the token budget. The docstring measures it: 24 keys × 106 pairs is
~22,500 tokens against a 20,000-token research context budget, so the rich tier had to be
rationed. But it is the honest answer to "are we checking all params": for 81% of the
watchlist, Earn checks six.

### 1.3 What the detectors use, and what is computed for nobody

Ten detectors are registered in `runs/signals/detectors.py`; **nine are enabled** (`ma_cross`
is `enabled: false` in `config/earn.yaml:366`, which means four of the 25 rich keys —
`ma_fast_1d`, `ma_slow_1d` and their two `_prev` twins — are computed every cycle and read by
nothing).

Counting them up: **9 of 25 rich keys are read by no detector** (`atr_pct_4h`, `rvol_1h`,
`vol_ann_20d`, `ma200_1d`, `ma200_dist_pct`, `drawdown_pct`, `oi_delta_pct`, `spread_bps`,
`news_count_24h`), plus the four MA keys behind a disabled detector. They are computed, put
in the prompt, and shown to the LLM screener — which is a legitimate use, but it means the
deterministic half of the system is reasoning over sixteen numbers, not twenty-five.

### 1.4 What the risk gate reads: no features at all

`strategies/riskgate.py` runs 27 checks (`CHECK_ORDER`, line 62). Grepping it for any alpha
feature — `features`, `rsi`, `atr`, `vol_z` — returns **zero** hits. The gate reads NAV,
limits, flags, freshness, mode, notional and correlation. It is a permission system, not an
opinion. That is by design and worth stating plainly, because it means no feature discussed
in this report could ever "block a bad buy" by being wired into the gate; a feature can only
change what the scanner surfaces or what the model proposes.

### 1.5 H2.8 confirmed by code read: spread and depth are recorded and ignored

- `ops/sql/knowledge.sql:16-27` — `book_snapshots` carries `best_bid`, `best_ask`, `mid`,
  `spread_bps`, `bid_depth_05pct`, `ask_depth_05pct`, `levels_json`.
- `runs/ingest.py:500` writes all seven columns every cycle.
- `bid_depth_05pct`, `ask_depth_05pct` and `levels_json` are read by **exactly nothing** in
  the repo outside the INSERT statement that writes them. Confirmed by grep across all
  non-test Python.
- `spread_bps` is read in exactly one place — `runs/signals/features.py:470`, one row, latest
  only — and **no detector consumes it.** It is decoration in the prompt.

So: **the owner's instinct is right. We record order-book depth continuously and have never
once looked at it.** But it cannot be backtested — the table is a live forward accrual (it was
748 rows at the last count in `docs/design/crisis-policy.md:919`) and the live DB is off-limits
this run. Anything built on it would be an unvalidated guess for months. This stays on the
"observe only" list, not the "wire it in" list.

### 1.6 How often we look — the frequency questions

| Job | Cadence | Consequence |
|---|---|---|
| `ingest` | every 15 min | candles are up to 15 min stale before the scanner sees them |
| `scanner` | every 5 min | 288 cycles/day; detectors read **closed candles only** |
| `healthcheck` | every 5 min | — |
| `nav_tick`, `reconcile` | every 15 min | — |
| `research_run` (the decision) | derived from `research.slots` | this is the only step that can change weights |
| `daily_review` | 21:30 Gulf | the learning loop |
| `review_run` | Sunday 20:00 Gulf | — |

The scanner runs every 5 minutes, but because it only reads **closed** candles, a 1h signal
can be up to 60 minutes old and a 1d signal up to 24 hours old at the moment it fires. The
five-minute cadence buys resolution the data does not have. That is not a defect to fix by
speeding anything up — Part 2 shows the cost floor makes sub-daily signals unusable anyway —
but it should not be described as "checking every five minutes" either.

---

## PART 2 — HALF TWO: testing what is missing

### 2.1 The data, and a fact the owner should know

The local feather store — the thing every Earn backtest reads — has **six columns**:

```
date  open  high  low  close  volume
```

Verified by dtype dump on `~/earn-run/data/binance/BTC_USDT-1h.feather` (79,661 rows,
2017-08-17 → 2026-09-23). There is **no `quote_volume`, no `taker_buy_base_volume`, no
`taker_buy_quote_volume`, no `number_of_trades`.** Binance's kline endpoint returns all four
and Earn throws them away at ingest. (The knowledge DB's `candles` table *does* carry
`quote_volume` — `ops/sql/knowledge.sql:10`, populated by `runs/ingest.py:473` — but the
feature builder reads base `volume`, not `quote_volume`, so dollar volume is in the database
and unused too.)

So every number below had to be fetched fresh from the public endpoint:

| Timeframe | Symbols | Span | Rows |
|---|---|---|---|
| 1d | 468 | 2017-08-17 → 2026-09-28 | 568,938 |
| 4h | 246 (top by 24h quote volume) | 2020-01-01 → 2026-09-28 | 1,736,029 |
| 1h | 119 (top by 24h quote volume) | 2022-01-01 → 2026-09-28 | 3,087,210 |

`/api/v3/exchangeInfo` reports **503 USDT spot pairs TRADING** with spot trading allowed,
against the 496 measured on 2026-09-30 by the profit audit — seven new listings in a day,
which is itself a useful number.

**Two universes throughout, because the distinction is the whole result:**

- **ALL** — every symbol with 20-day mean dollar volume ≥ $1M. Median ~170 names/day.
- **ELIGIBLE** — the *surviving* loser-avoidance filter from `growth-audit.md`: age ≥ 3 years
  **and** 20-day mean dollar volume ≥ $10M. Median **22 names/day**, 170 distinct symbols over
  the span. That 22 sits right where it should against the audited 31 authorisable names, which
  is a good independent check that I reconstructed Earn's actual reach.

### 2.2 The features built

| Name | Definition | New to Earn? |
|---|---|---|
| `tbr` | `taker_buy_quote / quote_volume` — the **buy share** of the bar | **yes** |
| `tbr_z20` | 20-bar z-score of `tbr` | **yes** |
| `ofi_z20` | z-score of `2·tbr − 1` — **identical to `tbr_z20` by construction**, reported once | yes |
| `tbv_z20` | z-score of log **buy-side** dollar volume — *the owner's "recent buy volume"* | **yes** |
| `dv_z20` | z-score of log total dollar volume | partly — Earn has `vol_z_1h` on *base* volume, 1h only, rich pairs only |
| `dv_acc` | log(mean 5-bar dollar volume) − log(mean 20-bar) — **volume acceleration** | **yes** |
| `nt_z20` | z-score of log `number_of_trades` | **yes** |
| `ats_z20` | z-score of log average trade size (`quote_volume / n_trades`) | **yes** |
| `div_down` | up bar **and** `dv_z20 < 0` — rising price, falling volume | **yes** |
| `div_up` | up bar **and** `tbv_z20 > 0` — rising price, rising buy volume | **yes** |

`ofi_z20` collapsing onto `tbr_z20` is not a nuisance — it is the first finding. An
order-flow-imbalance feature and a buy-share feature are the **same feature** under a linear
transform, so anyone who tests both and finds "two signals" has found one.

### 2.3 Finding #1 — the owner's named feature is a 0.99 copy of total volume

Cross-sectional Spearman correlation, ALL universe, 451,778 rows:

| | tbr | tbr_z20 | dv_z20 | **tbv_z20** | nt_z20 | ats_z20 | dv_acc | vol_60 | logdv |
|---|---|---|---|---|---|---|---|---|---|
| tbr | 1.000 | 0.861 | 0.107 | 0.195 | 0.099 | 0.057 | 0.035 | 0.021 | 0.116 |
| tbr_z20 | 0.861 | 1.000 | 0.098 | 0.194 | 0.092 | 0.048 | 0.011 | −0.004 | 0.040 |
| dv_z20 | 0.107 | 0.098 | 1.000 | **0.991** | 0.931 | 0.396 | 0.633 | −0.071 | 0.374 |
| **tbv_z20** | 0.195 | 0.194 | **0.991** | 1.000 | 0.923 | 0.393 | 0.625 | −0.070 | 0.371 |
| nt_z20 | 0.099 | 0.092 | 0.931 | 0.923 | 1.000 | 0.117 | 0.600 | −0.066 | 0.354 |
| ats_z20 | 0.057 | 0.048 | 0.396 | 0.393 | 0.117 | 1.000 | 0.256 | −0.011 | 0.158 |
| dv_acc | 0.035 | 0.011 | 0.633 | 0.625 | 0.600 | 0.256 | 1.000 | −0.082 | 0.274 |

**Buy-side dollar volume correlates 0.991 with total dollar volume, and 0.923 with the trade
count.** When volume goes up, buy volume goes up, because roughly half of every bar is buying
and the half barely moves. "Recent buy volume" is total volume wearing a different label. The
pre-registration said a feature that merely proxies volume is worth nothing; this one proxies
it at 0.991.

The *share* is different: `tbr` correlates only **0.107** with `dv_z20` and **0.021** with
60-day volatility. That is a genuinely new, nearly orthogonal number. So the interesting
question is not "how much buying" but "what fraction of the bar was buying" — and that is what
the rest of this report tests.

### 2.4 Finding #2 — rank IC at 1d: everything works where Earn cannot trade

Spearman rank IC per day, Newey-West t with 10 lags on the daily IC series. `ofi_z20` omitted
(identical to `tbr_z20`).

**ALL universe (dv20 ≥ $1M, ~170 names/day, 2,690 days):**

| Feature | IC(fwd 1d) | NW t | IC first half | IC second half | sign stable |
|---|---|---|---|---|---|
| tbr | −0.0083 | −3.27 | −0.0013 | −0.0152 | yes |
| **tbr_z20** | **−0.0162** | **−7.37** | −0.0157 | −0.0168 | yes |
| **dv_z20** | **−0.0571** | **−17.33** | −0.0580 | −0.0562 | yes |
| **tbv_z20** | **−0.0573** | **−17.40** | −0.0583 | −0.0563 | yes |
| **nt_z20** | **−0.0571** | **−17.20** | −0.0589 | −0.0553 | yes |
| ats_z20 | −0.0294 | −10.77 | −0.0304 | −0.0284 | yes |
| dv_acc | −0.0425 | −13.25 | −0.0406 | −0.0444 | yes |

| Feature | IC(fwd 5d) | NW t | first half | second half | sign stable |
|---|---|---|---|---|---|
| tbr | +0.0061 | 1.81 | +0.0180 | −0.0058 | **no** |
| tbr_z20 | −0.0079 | −3.21 | −0.0069 | −0.0088 | yes |
| dv_z20 | −0.0464 | −9.73 | −0.0443 | −0.0484 | yes |
| tbv_z20 | −0.0462 | −9.85 | −0.0442 | −0.0483 | yes |
| nt_z20 | −0.0470 | −9.63 | −0.0440 | −0.0500 | yes |
| ats_z20 | −0.0239 | −5.66 | −0.0255 | −0.0223 | yes |
| dv_acc | −0.0365 | −6.38 | −0.0366 | −0.0365 | yes |

**ELIGIBLE universe (age ≥ 3y, dv ≥ $10M, ~22 names/day, 1,262 days):**

| Feature | IC(fwd 1d) | NW t | IC(fwd 5d) | NW t | sign stable (1d) |
|---|---|---|---|---|---|
| tbr | −0.0041 | −0.80 | **+0.0192** | **2.93** | yes |
| tbr_z20 | −0.0076 | −1.48 | +0.0071 | 1.10 | **no** |
| dv_z20 | −0.0280 | −3.88 | −0.0172 | −1.73 | yes |
| tbv_z20 | −0.0291 | −4.09 | −0.0165 | −1.66 | yes |
| nt_z20 | −0.0268 | −3.81 | −0.0161 | −1.67 | yes |
| ats_z20 | −0.0216 | −3.60 | −0.0211 | −2.42 | yes |
| dv_acc | −0.0242 | −3.41 | −0.0058 | −0.49 | yes |

Read those two blocks together. In the broad universe every volume feature has an enormous,
sample-stable t — and the sign is **negative**: *a bar with unusually high volume is followed
by a lower return.* Cut to the 22 names Earn may actually authorise and the t-statistics fall
by a factor of four. `tbr_z20`, the one genuinely new number, goes from t = −7.37 to
t = −1.48 and its sign flips between sample halves — a clean hit on H2.1's falsifier.

This is the same shape the profit audit found on returns: the effect lives in the 383 coins
Earn ignores for good reason.

### 2.5 Finding #3 — the deciding test. Nothing survives the controls.

Fama-MacBeth: a cross-sectional OLS every day, features standardised to 1 cross-sectional sd,
returns demeaned per day (day fixed effects), coefficients averaged with Newey-West t.
Targets winsorised 1/99 per day — without that, a single microcap 900% day makes every
coefficient meaningless. Controls: log dollar volume, 60-day vol, log age, prior return.
Coefficients in **bps of next-period return per 1 sd of feature**.

**ALL universe:**

| Feature | β raw (bps) | t raw | **β after controls** | **t after controls** |
|---|---|---|---|---|
| tbr | −0.92 | −1.02 | +0.70 | 0.83 |
| **tbr_z20** | **−2.36** | **−2.48** | **+0.46** | **0.54** |
| dv_z20 | −11.42 | −7.25 | −7.66 | **−5.31** |
| tbv_z20 | −11.38 | −7.26 | −7.54 | **−5.22** |
| nt_z20 | −12.14 | −7.91 | −8.17 | **−5.78** |
| ats_z20 | −3.02 | −2.56 | −1.56 | −1.58 |
| dv_acc | −8.29 | −5.28 | −7.81 | **−5.09** |

**ELIGIBLE universe — the one that decides:**

| Feature | target | β raw (bps) | t raw | **β after controls** | **t after controls** |
|---|---|---|---|---|---|
| tbr | fwd1 | +0.23 | 0.11 | +0.04 | 0.02 |
| tbr_z20 | fwd1 | −0.39 | −0.17 | −0.25 | −0.13 |
| dv_z20 | fwd1 | −2.87 | −0.85 | −1.88 | −0.61 |
| tbv_z20 | fwd1 | −3.00 | −0.88 | −2.05 | −0.65 |
| nt_z20 | fwd1 | −3.67 | −1.14 | −4.08 | −1.35 |
| ats_z20 | fwd1 | −4.56 | **−1.99** | −0.14 | **−0.07** |
| dv_acc | fwd1 | −2.75 | −0.75 | −3.11 | −0.88 |
| tbr | fwd5 | +9.32 | 1.50 | +7.18 | **1.08** |
| tbr_z20 | fwd5 | +8.23 | 1.19 | +5.91 | 0.92 |
| dv_z20 | fwd5 | +1.65 | 0.14 | +1.59 | 0.14 |
| tbv_z20 | fwd5 | +1.81 | 0.15 | +2.05 | 0.18 |
| nt_z20 | fwd5 | +0.49 | 0.05 | −2.03 | −0.20 |
| ats_z20 | fwd5 | −10.59 | −1.27 | −5.64 | −0.75 |
| dv_acc | fwd5 | −7.42 | −0.52 | −6.65 | −0.49 |

**Fourteen features × horizons in the tradeable universe. The largest \|t\| after controls is
1.08. The pre-registered bar was 2.0.** H2.5 is refuted outright for every new feature.

Two things worth saying about the ALL column. First, `tbr_z20` — which had t = −7.37 as a raw
rank IC — falls to **t = 0.54** once you control for volume, volatility, age and reversal. Its
apparent signal *was* those four things. Second, what does survive there (dv_z20, tbv_z20,
nt_z20, dv_acc at t ≈ −5) survives as one feature, not four: they correlate 0.92–0.99 with each
other. It is "this bar traded unusually heavily, expect a lower return tomorrow" — a known
short-term reversal effect, measured here in coins Earn is refused from trading.

Note `ats_z20` in the ELIGIBLE fwd1 row: raw t = −1.99, controlled t = −0.07. Average trade
size *looks* like the most promising new microstructure feature and is the most completely
explained away by volume. That is exactly the trap the incremental test exists to catch.

### 2.6 Finding #4 — the "peak or drop" test, and its sign is backwards

This is the owner's question stated directly. Groups are formed on **up bars only**; the
target is the next 5 days' return, winsorised 1/99 per day.

| Universe | Group | mean fwd-5d return | NW t | days | observations |
|---|---|---|---|---|---|
| ALL | A: rising price + **falling** volume | **+0.197%** | 0.56 | 2,854 | 112,596 |
| ALL | B: rising price + **rising buy** volume | **−0.044%** | −0.12 | 3,110 | 104,398 |
| ALL | C: rising price + rising volume + **falling buy share** | +0.321% | 0.81 | 2,810 | 32,014 |
| ALL | baseline: every up bar | +0.198% | 0.54 | 3,201 | 214,518 |
| ALL | **SPREAD A − B (pre-registered)** | **+0.454%** | **3.11** | 2,766 | — |
| ELIGIBLE | A | −0.033% | −0.09 | 1,680 | 14,463 |
| ELIGIBLE | B | +0.028% | 0.06 | 1,900 | 14,070 |
| ELIGIBLE | C | +0.209% | 0.38 | 1,353 | 4,088 |
| ELIGIBLE | baseline | +0.204% | 0.53 | 2,096 | 28,181 |
| ELIGIBLE | **SPREAD A − B** | **−0.006%** | **−0.02** | 1,488 | — |

**The classic chart-reading rule is backwards.** "Rising price on falling volume means the
rally is exhausted" predicts A < B. Measured: A **beat** B by 0.45% over five days in the broad
universe, at t = 3.11. The pre-registration named an inverted sign as a falsifier precisely so
this could not be re-told as a discovery. H2.3 is refuted.

And in the universe Earn can trade, the spread is **−0.006% at t = −0.02** — not a weak effect,
an absent one. Roughly 14,000 observations on each side and the two groups are
indistinguishable.

The honest reading: on an up bar, whether volume was rising or falling, and whether the buying
share was rising or falling, tells you **nothing** about whether the coin is about to peak. The
baseline up bar returns +0.20% over five days; every conditioning we tried lands within noise
of that, and 0.20% does not pay a 0.30% round trip.

### 2.7 Finding #5 — purged walk-forward confirms the split

Five contiguous time folds, one bar purged at each boundary, mean rank IC per fold, target
fwd-1d.

Fold ranges (ALL): 1 = 2017-08-26→2019-06-20, 2 = →2021-04-14, 3 = →2023-02-07,
4 = →2024-12-02, 5 = →2026-09-27.

**ALL universe:**

| Feature | f1 | f2 | f3 | f4 | f5 | same sign | last fold |
|---|---|---|---|---|---|---|---|
| tbr | +0.0096 | +0.0065 | −0.0124 | −0.0160 | −0.0141 | **3/5** | −0.0141 |
| tbr_z20 | +0.0044 | −0.0142 | −0.0204 | −0.0185 | −0.0147 | 4/5 | −0.0147 |
| dv_z20 | −0.0360 | −0.0351 | −0.0825 | −0.0582 | −0.0527 | **5/5** | −0.0527 |
| tbv_z20 | −0.0374 | −0.0356 | −0.0825 | −0.0585 | −0.0528 | **5/5** | −0.0528 |
| nt_z20 | −0.0624 | −0.0319 | −0.0840 | −0.0581 | −0.0514 | **5/5** | −0.0514 |
| ats_z20 | +0.0021 | −0.0283 | −0.0372 | −0.0325 | −0.0226 | 4/5 | −0.0226 |
| dv_acc | +0.0067 | −0.0296 | −0.0565 | −0.0459 | −0.0425 | 4/5 | −0.0425 |

**ELIGIBLE universe** (fold 1 is empty — no coin was ≥3 years old with ≥$10M volume before
2019; the eligible panel starts 2020-08-15, which is itself worth knowing):

| Feature | f1 | f2 | f3 | f4 | f5 | same sign | last fold |
|---|---|---|---|---|---|---|---|
| tbr | — | −0.0023 | +0.0073 | −0.0231 | +0.0085 | **2/5** | +0.0085 |
| tbr_z20 | — | −0.0453 | −0.0063 | −0.0229 | +0.0155 | **3/5** | +0.0155 |
| dv_z20 | — | −0.0349 | −0.0502 | −0.0296 | −0.0091 | 4/5 | −0.0091 |
| tbv_z20 | — | −0.0463 | −0.0501 | −0.0325 | −0.0074 | 4/5 | −0.0074 |
| nt_z20 | — | −0.0372 | −0.0495 | −0.0269 | −0.0089 | 4/5 | −0.0089 |
| ats_z20 | — | −0.0025 | −0.0357 | −0.0270 | −0.0099 | 4/5 | −0.0099 |
| dv_acc | — | −0.0423 | −0.0443 | −0.0176 | −0.0147 | 4/5 | −0.0147 |

Both buy-share features fail H2.6 in the tradeable universe (2/5 and 3/5, and both turn
*positive* in the most recent fold after being negative earlier). The volume features hold
4/5 but decay monotonically toward zero: `dv_z20` runs −0.0349 → −0.0502 → −0.0296 → −0.0091.
By the most recent fold it is one fifth of its 2021-23 strength. That decay pattern is what a
crowded, well-known short-term-reversal effect looks like as the market absorbs it.

### 2.8 Finding #6 — order-flow persistence at 4h and 1h, where it should be strongest

If the buy side carries microstructure information, it should show up at short horizons. It
does — and then it dies in the tradeable universe anyway.

**4h (246 symbols, 2020-01-01 → 2026-09-28, 1,736,029 rows; eligible median 30 names/bar):**

| Universe | Target | Feature | IC | NW t | bars | IC h1 | IC h2 |
|---|---|---|---|---|---|---|---|
| ALL | next 4h | tbr | −0.0176 | **−18.35** | 14,771 | −0.0209 | −0.0142 |
| ALL | next 4h | **tbr_z20** | **−0.0193** | **−20.37** | 14,752 | −0.0235 | −0.0151 |
| ALL | next 4h | dv_z20 | −0.0148 | −12.43 | 14,752 | −0.0134 | −0.0163 |
| ALL | next 4h | tbv_z20 | −0.0186 | −15.59 | 14,752 | −0.0184 | −0.0188 |
| ALL | next 4h | nt_z20 | −0.0164 | −13.73 | 14,752 | −0.0168 | −0.0161 |
| ALL | next 4h | ats_z20 | −0.0027 | −2.68 | 14,752 | +0.0018 | −0.0072 |
| ALL | next 4h | dv_acc | −0.0074 | −6.49 | 14,752 | −0.0036 | −0.0112 |
| ALL | next 24h | tbr_z20 | −0.0110 | −10.40 | 14,747 | −0.0122 | −0.0097 |
| ALL | next 24h | dv_acc | −0.0184 | −7.94 | 14,747 | −0.0192 | −0.0177 |
| **ELIGIBLE** | next 4h | tbr | −0.0024 | **−1.02** | 6,394 | −0.0048 | −0.0000 |
| **ELIGIBLE** | next 4h | tbr_z20 | −0.0064 | **−2.67** | 6,394 | −0.0098 | −0.0029 |
| ELIGIBLE | next 4h | dv_z20 | −0.0027 | −1.01 | 6,394 | −0.0044 | −0.0010 |
| ELIGIBLE | next 4h | tbv_z20 | −0.0030 | −1.14 | 6,394 | −0.0054 | −0.0005 |
| ELIGIBLE | next 4h | ats_z20 | −0.0053 | −2.12 | 6,394 | −0.0065 | −0.0040 |
| ELIGIBLE | next 24h | tbr_z20 | −0.0047 | −1.95 | 6,389 | −0.0060 | −0.0035 |
| ELIGIBLE | next 24h | ats_z20 | −0.0087 | −2.48 | 6,389 | −0.0034 | −0.0141 |
| ELIGIBLE | next 24h | dv_acc | −0.0096 | −1.75 | 6,389 | −0.0167 | −0.0026 |

**1h (119 symbols, 2022-01-01 → 2026-09-28, 3,087,210 rows; eligible median 29 names/bar):**

| Universe | Target | Feature | IC | NW t | bars | IC h1 | IC h2 |
|---|---|---|---|---|---|---|---|
| ALL | next 1h | tbr | −0.0169 | **−23.94** | 41,541 | −0.0215 | −0.0124 |
| ALL | next 1h | **tbr_z20** | **−0.0182** | **−25.89** | 41,503 | −0.0237 | −0.0128 |
| ALL | next 1h | dv_z20 | −0.0078 | −10.71 | 41,523 | −0.0090 | −0.0067 |
| ALL | next 1h | tbv_z20 | −0.0129 | −17.39 | 41,523 | −0.0155 | −0.0103 |
| ALL | next 1h | nt_z20 | −0.0080 | −10.98 | 41,523 | −0.0088 | −0.0071 |
| ALL | next 1h | ats_z20 | −0.0032 | −5.04 | 41,503 | −0.0036 | −0.0027 |
| ALL | next 1h | dv_acc | −0.0075 | −11.02 | 41,523 | −0.0096 | −0.0055 |
| ALL | next 6h | tbr_z20 | −0.0114 | −15.65 | 41,499 | −0.0153 | −0.0075 |
| **ELIGIBLE** | next 1h | tbr | −0.0054 | −3.20 | 14,754 | −0.0093 | −0.0015 |
| **ELIGIBLE** | next 1h | **tbr_z20** | **−0.0063** | **−3.69** | 14,754 | −0.0095 | −0.0031 |
| ELIGIBLE | next 1h | dv_z20 | −0.0027 | −1.46 | 14,754 | −0.0065 | +0.0011 |
| ELIGIBLE | next 1h | tbv_z20 | −0.0040 | −2.20 | 14,754 | −0.0091 | +0.0011 |
| ELIGIBLE | next 1h | nt_z20 | −0.0039 | −2.01 | 14,754 | −0.0071 | −0.0006 |
| ELIGIBLE | next 6h | tbr_z20 | −0.0036 | −1.88 | 14,749 | −0.0013 | −0.0058 |
| ELIGIBLE | next 6h | dv_acc | +0.0017 | 0.48 | 14,749 | +0.0009 | +0.0025 |

This is the strongest case the buy side makes anywhere in the study, so state it fairly:

- At 1h and 4h, in the broad universe, **`tbr_z20` is the single best feature** — better than
  total volume (1h: IC −0.0182 vs −0.0078; 4h: −0.0193 vs −0.0148). So the buy *share* is real
  information that total volume does not contain, exactly as the 0.107 correlation implied.
- At 1h in the **tradeable** universe it clears the multiplicity bar: t = −3.69 against 3.3.
  It is the only new feature anywhere in this study to do so.
- **And its effect size is −0.0063, which is four times too small to pay for itself.** Section
  2.9 does that arithmetic. H2.2's magnitude falsifier (\|IC\| ≥ 0.02) fails even in the broad
  universe, where the best is 0.0193.
- Both halves decay: 1h ALL runs −0.0237 → −0.0128; 1h ELIGIBLE runs −0.0095 → −0.0031, a 3×
  fall. Whatever this is, it is being arbitraged away.

### 2.9 Finding #7 — the cost arithmetic that ends the discussion

In the eligible universe the mean cross-sectional standard deviation of the next day's return
is **3.24%**. For a daily-rebalanced decile long/short, the expected gross spread from rank IC
*x* is approximately 2 × 1.755 × *x* × σ.

| rank IC | gross spread/day | net of 0.30% |
|---|---|---|
| 0.006 (best 1h eligible) | 0.068% | **−0.232%** |
| 0.016 (best 1d ALL, tbr_z20) | 0.182% | **−0.118%** |
| 0.020 (the pre-registered bar) | 0.227% | **−0.073%** |
| 0.050 | 0.568% | +0.268% |
| 0.086 | 0.978% | +0.678% |

> **Break-even rank IC for a daily-rebalanced decile book: 0.0264.**

Every new feature measured in the tradeable universe comes in at 0.002–0.019 — **between 1.4×
and 13× below break-even.** This is the same 13.7× shortfall `exit-and-horizon-2026-09-29.md`
found on the shipped 1h rule, arrived at from a completely different direction.

And the actual books, run over 2,235 days of the eligible panel (2020-08-15 → 2026-09-27),
costed at 30 bps per full turn:

| Book | days | ann. Sharpe | CAGR | max DD |
|---|---|---|---|---|
| **BASELINE BTC buy-and-hold (same days)** | 2,235 | **0.843** | **+37.55%** | −76.6% |
| BASELINE equal-weight all eligible, no cost | 2,235 | 0.519 | +11.22% | −89.1% |
| BASELINE equal-weight all eligible, ~1 turn/month | 2,235 | 0.420 | +3.39% | −92.6% |
| tbr_z20 bottom decile GROSS | 2,235 | 0.510 | +9.62% | −88.3% |
| tbr_z20 bottom decile **NET** (81%/day turnover) | 2,235 | **−0.596** | −54.77% | −99.9% |
| tbr_z20 top decile GROSS | 2,235 | 0.609 | +16.34% | −92.5% |
| tbr_z20 top decile **NET** (82%/day) | 2,235 | **−0.402** | −52.57% | −99.9% |
| tbr bottom decile GROSS | 2,235 | 0.145 | −13.37% | −96.1% |
| tbr bottom decile **NET** (72%/day) | 2,235 | **−0.988** | −60.88% | −99.9% |
| tbr top decile GROSS | 2,235 | 0.570 | +13.27% | −92.2% |
| tbr top decile **NET** (74%/day) | 2,235 | −0.388 | −49.59% | −99.9% |
| dv_z20 bottom decile NET (59%/day) | 2,235 | −0.566 | −57.60% | −99.9% |
| dv_z20 top decile NET (59%/day) | 2,235 | −0.213 | −53.06% | −99.9% |
| dv_acc bottom decile NET (23%/day) | 2,235 | −0.100 | −37.96% | −99.3% |
| dv_acc top decile NET (24%/day) | 2,235 | **+0.035** | −42.20% | −99.9% |
| **OVERLAY: all eligible EXCLUDING top-decile buy share, gross** | 2,235 | 0.486 | +8.52% | −89.5% |
| OVERLAY: same, net (10%/day swapped) | 2,235 | 0.339 | −2.75% | −94.1% |

Three things to notice. **Not one net book is positive** — the best is `dv_acc` top decile at
Sharpe 0.035 against a deflated hurdle of 2.32 and a BTC baseline of 0.843. Even **gross**, no
book beats BTC hold: best gross is `tbr_z20` top decile at 0.609 against 0.843. And the
realistic overlay — the one form a long-only spot book could actually express, "hold every
eligible name except the ones with the highest buy share" — is **worse than holding all of
them** (0.486 vs 0.519 gross). The filter destroys value even before it is paid for.

That last line also re-confirms `dip-strategy.md §0.2` from a new angle: in a long-only spot
book at full exposure, a buy signal has nowhere to go. The only expressible form of a positive
signal is *not excluding* something, and here the exclusion was already wrong.

### 2.10 Finding #8 — the drawdown question, and the feature Earn already has

The owner's "how well will it drop" deserves a direct test, and it connects to the one
unfinished ML thread in `ml-forecast.md`: a cross-sectional risk ordering at AUC 0.617 that was
never wired to anything. Target: does the close fall ≥20% below today's close at any point in
the next 7 days? Base rate 7.24% in the eligible set (4,238 events in 58,535 rows), 9.04% in ALL.

| Universe | Feature | AUC | within vol_60 decile |
|---|---|---|---|
| ALL | **vol_60** | **0.6564** | — |
| ALL | tbr | 0.5093 | 0.5031 |
| ALL | tbr_z20 | 0.4995 | 0.4983 |
| ALL | dv_z20 | 0.5271 | 0.5439 |
| ALL | tbv_z20 | 0.5262 | 0.5425 |
| ALL | nt_z20 | 0.5272 | 0.5454 |
| ALL | ats_z20 | 0.5152 | 0.5155 |
| ALL | dv_acc | 0.5201 | 0.5401 |
| ALL | prior return (lower = riskier) | 0.4962 | — |
| **ELIGIBLE** | **vol_60** | **0.7242** | — |
| ELIGIBLE | tbr | 0.5096 | 0.5053 |
| ELIGIBLE | tbr_z20 | 0.5015 | 0.5053 |
| ELIGIBLE | dv_z20 | 0.5292 | 0.5524 |
| ELIGIBLE | tbv_z20 | 0.5285 | 0.5523 |
| ELIGIBLE | nt_z20 | 0.5276 | 0.5504 |
| ELIGIBLE | ats_z20 | 0.5346 | 0.5206 |
| ELIGIBLE | **dv_acc** | 0.5297 | **0.5599** |
| ELIGIBLE | prior return | 0.4925 | — |

**The taker-buy features are at chance for predicting a drop: 0.5015 and 0.5096.** After all
the machinery, the buy side does not see a crash coming.

But look at the first row of the eligible block. **60-day realised volatility alone scores AUC
0.7242** on Earn's own tradeable universe — against the hand-set flag's 0.543 and the nine-model
ML zoo's best of 0.617 recorded in `ml-forecast.md`. Using cross-sectional daily percentile
ranks instead of raw levels (no look-ahead), the same feature scores **0.6540** with:

| Score | AUC | fold 1 | fold 2 | fold 3 | fold 4 | fold 5 | top-decile lift | bottom-decile lift |
|---|---|---|---|---|---|---|---|---|
| **vol_60 percentile** | **0.6540** | 0.6026 | 0.6588 | 0.6458 | 0.6244 | **0.7440** | **2.14** | **0.15** |
| dv_acc percentile | 0.5290 | — | — | — | — | — | 1.68 | 1.08 |
| dv_z20 percentile | 0.5283 | — | — | — | — | — | — | — |
| 50/50 vol_60 + dv_acc | 0.6207 | 0.5368 | 0.6190 | 0.6122 | 0.5986 | 0.7021 | 2.00 | 0.47 |
| ⅓ each vol_60 + dv_acc + dv_z20 | 0.5891 | 0.5075 | 0.5787 | 0.5872 | 0.5723 | 0.6583 | 1.94 | 0.59 |

Fold ranges: 1 = 2020-08-15→2021-11-05, 2 = →2023-01-26, 3 = →2024-04-17, 4 = →2025-07-08,
5 = 2025-07-09→2026-09-28.

**Blending anything into vol_60 makes it worse.** 0.6540 → 0.6207 → 0.5891. The
within-vol-decile numbers above (`dv_acc` at 0.5599) say volume acceleration carries *some*
independent risk information — and it holds in 5/5 purged folds (0.5322, 0.6326, 0.5099,
0.5118, 0.5842) — but not enough to survive being combined at any weight I tried. So the honest
recommendation is the opposite of "add a feature": **use the one already there, alone.**

`vol_ann_20d` is in `CHEAP_KEYS`. It is computed for **all 107 watchlist names, every cycle,
today**, and read by no detector at all — only by `rank_watchlist`'s attention score, where it
carries a weight of 0.1. This is the same conclusion `ml-forecast.md` reached from the return
side ("a single feature, −vol_60 cross-sectionally, beat the best model at 4 of 6 targets"),
now reached from the drawdown side.

### 2.11 Finding #9 — the rally question

Same method, opposite target: does the close rise ≥50% above today's close at any point in the
next 30 days? Base rate 8.63% eligible, 12.33% ALL.

| Universe | Feature | AUC | within vol_60 decile |
|---|---|---|---|
| ALL | vol_60 | **0.5950** | — |
| ALL | dv_acc | 0.5627 | **0.5728** |
| ALL | tbv_z20 | 0.5568 | 0.5653 |
| ALL | dv_z20 | 0.5567 | 0.5653 |
| ALL | nt_z20 | 0.5542 | — |
| ALL | ats_z20 | 0.5283 | 0.5303 |
| ALL | prior return | 0.5192 | — |
| ALL | **tbr** | **0.5132** | 0.5104 |
| ALL | **tbr_z20** | **0.5086** | 0.5092 |
| ELIGIBLE | vol_60 | **0.5905** | — |
| ELIGIBLE | **dv_acc** | **0.5846** | **0.5764** |
| ELIGIBLE | dv_z20 | 0.5704 | 0.5723 |
| ELIGIBLE | tbv_z20 | 0.5702 | 0.5719 |
| ELIGIBLE | nt_z20 | 0.5653 | — |
| ELIGIBLE | ats_z20 | 0.5434 | 0.5574 |
| ELIGIBLE | prior return | 0.5244 | — |
| ELIGIBLE | **tbr** | **0.5150** | 0.5111 |
| ELIGIBLE | **tbr_z20** | **0.5100** | 0.5134 |

**The buy side cannot see a rally coming either: AUC 0.5100 and 0.5150, i.e. a coin flip.**

The two features that do order rally probability are volatility (0.5905) and volume
acceleration (0.5846) — and they order *drawdown* probability with the **same sign**
(vol_60 0.7242 for a −20%, 0.5905 for a +50%). They are not finding rallies; they are finding
**volatility**, which resolves both ways. `growth-audit.md` already measured what happens next:
77.5% of +100% run-ups are given back within 60 days. A detector that flags "this coin might
move 50%" without saying which way is not an entry signal; at best it is a position-size input,
and that is what `vol_surface`'s scalar already is.

### 2.12 What I did not test, and the one thing that cannot be tested

- **Spread and depth** (H2.8): declared untestable in advance and confirmed by code read. We
  write `bid_depth_05pct`, `ask_depth_05pct` and `levels_json` every ingest cycle and read them
  nowhere. `spread_bps` is read once, shown to the model, and consumed by no detector. There is
  no history to backtest and the live accrual was 748 rows at last count. **Do not wire it;
  keep accruing it and revisit when there are two years of it.** The cheap fix meanwhile is to
  stop pretending it is a feature.
- **Live journal data**: not touched, by rule. Nothing in this report depends on it.

---

## PART 3 — the verdict, and what I would actually change

### 3.1 The answer to the owner's question

> *"are we checking all params like recent buy volume or volume and other params to know how
> well it will peak or drop"*

**No — and I went and measured the missing ones on 468 coins over nine years, and they are not
worth adding.**

1. **Recent buy volume** is 0.991 correlated with total volume. It is the same number.
2. **Buy *share*** — the genuinely new number, 0.107 correlated with volume — is statistically
   powerful (1h t = −25.9, 4h t = −20.4) in the 383 coins Earn is refused from trading, and
   fails every pre-registered falsifier in the 31 it may trade: sign flips between sample
   halves, 2/5 and 3/5 in purged folds, and t = 1.08 at best after controls against a bar of
   2.0.
3. Its direction is also **the opposite of intuition**: heavy buying predicts a *lower* next
   bar, not a higher one. Anyone wiring "lots of buying = going up" would have wired it
   backwards.
4. **Volume-price divergence does not work.** Rising price on falling volume *outperformed*
   rising price on rising buy volume by 0.45% over five days (t = 3.11) in the broad universe,
   and by −0.006% (t = −0.02) in the tradeable one. The pre-registration called the inverted
   sign a falsifier so it could not be retold as a finding.
5. **Nothing in the buy side predicts a peak or a drop**: AUC 0.5015–0.5150 against a base rate
   of 7.24%, which is a coin flip.
6. **The cost floor kills all of it regardless.** Break-even rank IC for a daily-rebalanced
   decile book in Earn's eligible universe is **0.0264**. The best number any new feature
   produced there is 0.019, and in the tradeable set 0.006. Every net book is negative; the best
   is Sharpe 0.035 against a hurdle of 2.32 and BTC hold at 0.843.

### 3.2 What I would change — four things, none of them a new feature

1. **Wire `vol_ann_20d` to something.** It is already computed for all 107 watchlist names. As
   a cross-sectional percentile it orders 7-day −20% drawdowns at **AUC 0.6540, 5/5 purged
   folds, top-decile lift 2.14, bottom-decile lift 0.15, and its best fold is the most recent
   one (0.744)** — above the hand-set 0.543 and the ML zoo's 0.617. This is the unfinished
   `ml-forecast.md` thread, and the feature it needs is already in the cheap tier. That is a
   tier-1 change to one detector, not a research project.
   *Caveat to check before acting: my target may not be defined identically to the 0.617 study's
   — see `claims_to_verify`.*
2. **Delete four features or enable the detector that reads them.** `ma_fast_1d`,
   `ma_slow_1d` and their two `_prev` twins are computed every cycle for every rich pair and
   read only by `ma_cross`, which is `enabled: false`. Same for `drawdown_pct`, an exact
   duplicate of `dip_from_high_pct` occupying a key in a token-constrained block.
3. **Stop showing `spread_bps` and `oi_delta_pct` as if they were signals.** No detector reads
   either. They cost prompt tokens in a block that had to ration the rich tier to 20 of 107
   pairs. Either move them to a diagnostics view or spend those tokens on more pairs.
4. **Check the `volume_spike` direction label.** `runs/signals/detectors.py:411` sets
   `direction = UP` when the spiking bar's 1h return is positive. Cross-sectionally, a
   high-volume bar is followed by a **lower** return (ALL 1d IC −0.057 at t = −17.3; 1h −0.008
   at t = −10.7). The detector's own strength score is fine; the direction label it hands the
   model points the wrong way. Note the effect does not survive in the tradeable universe
   either, so the correct fix may be to drop the direction label rather than invert it.

### 3.3 The one honest positive for the owner

The buy side is not a dead end because it is noise — it is a dead end because it is *small*.
`tbr_z20` at 1h is, on the raw statistics, the strongest new feature this study found
(t = −25.9 over 41,503 hourly cross-sections, sign stable, and it beats total volume). If Earn
were a market-maker paying 0 bps and turning over every hour, it would be worth building. At
30 bps a round trip and 22 tradeable names, it is worth 0.068% a day against a 0.30% toll.

That is not a failure of the idea. It is the cost floor doing its job, and it is the same wall
the last four studies hit from four different directions.

---

## Appendix — reproduction

Everything under `~/earn-scratch/track2/` (WSL, outside `~/earn-run`, nothing written to the
repo but this file):

| File | What |
|---|---|
| `PREREG.md` | the sealed pre-registration |
| `fetch.py` | public REST fetcher (`exchangeInfo`, `klines`, `ticker/24hr`) |
| `lib.py` | features, Newey-West t, rank IC, Fama-MacBeth |
| `stage1.py` … `stage9.py` | the measurements, in the order run |
| `raw/` | 468 × 1d, 246 × 4h, 119 × 1h parquet files, 429 MB |
| `panel1d.parquet` | the daily feature panel, 568,938 rows |
| `ic_1d.csv`, `ic_4h.csv`, `ic_1h.csv`, `fm_1d.csv`, `fm_1d_wins.csv` | result tables |

Load discipline: `nice -n 10` on every panel-wide job, one at a time, `/proc/loadavg` checked
before each; peak 1-minute load observed 6.03, never sustained above 6. No GPU. No live
database opened.
