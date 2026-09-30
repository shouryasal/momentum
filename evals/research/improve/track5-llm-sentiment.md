# Track 5 — internet search, news and social sentiment, and what else the LLM could do

Workspace `im5`. Run date 2026-09-30. All numbers below were computed in this run from
`~/earn-panels/panel_1d.parquet` (747 USDT tickers, survivorship-free, 2017-08 → 2026-09),
public keyless REST endpoints, and the repo's own committed config. **No live database was
opened, copied or read at any point** — not `knowledge/earn.db`, not `journal/journal.db`,
not the `ft_userdata` sqlite files. Nothing was written under `~/earn-run`. No order, no
config change, no commit.

Cost floor on every number: **0.30% round trip** (10 bps fee + 5 bps slippage per side).
Sharpe is the repo's arithmetic Sharpe, `mean/std × sqrt(365)`, never CAGR/vol.

---

## 0. The one-paragraph answer

**Part one is a refusal, and it is a well-evidenced one.** The only free, keyless,
point-in-time attention series that exists for crypto is Wikipedia pageviews. It is clean as
an *endpoint* and broken as a *series*, because the article identity is not stable over time
and does not exist at all for a third of the coins Earn cares about. Where it is clean —
Bitcoin and Ethereum, 3,289 days each — attention does not predict forward return
(NW(7) t = +0.82 and +0.09), does not predict forward volatility once trailing volatility is
in the model (out-of-sample ΔR² = **−0.0047** on BTC, +0.0152 on ETH, both under the
pre-registered 0.02 bar), and does not order drawdown risk (AUC **0.502**, a coin flip,
against trailing volatility's 0.609 on the same panel). Google Trends is rejected as
unbacktestable by construction. Reddit's archive is gone. GitHub gives 52 weeks. The Fear &
Greed index is 60% price arithmetic wearing a sentiment label, and correlates +0.428 with the
past week's return. Earn's own news archive **cannot be rebuilt** — the nine whitelisted
feeds carry between 10 and 50 items, i.e. one to two days.

**Part two is where the value is.** Three LLM jobs pass Earn's discipline (a model may only
produce something host code re-checks), and one of them is worth more than everything in part
one: reading a Binance delisting or halt notice into a structured fact. Measured on the 195
pairs that left the panel, the **final 30 days of a delisted pair's life is a median −47.3%**
and the **final 7 days −18.5%**, 74% of them negative. Nothing in the 17-check gate looks at
venue status or the USDT peg today. That is the gap.

---

## 1. Pre-registration (sealed before any number was computed)

`hypothesis-lab` requires the falsifier before the measurement. The pre-registration was
written to `<scratch>/im5/prereg.json` and sealed with sha256
**`0873a2481f088ce4a9715e43973115a0e10a020fab44d2bfa8485bc6d326d00f`** before any of §3–§6
was run.

> **Deviation, stated:** the skill's own `hypothesis.py open` writes to
> `knowledge/state/hypotheses/<id>.json`, which is a tier-2 path and also under the live state
> root. This run sealed the pre-registration in its scratchpad with the same sha256 discipline
> instead of writing there. The seal is reproducible: `sha256sum prereg.json`.

| | statement | falsifier |
|---|---|---|
| **H0** | A point-in-time-honest Wikipedia attention series is retrievable for a materially survivorship-free share of the 747-ticker panel. | Retrievable coverage of **delisted** tickers below half the coverage of surviving tickers ⇒ contaminated at source, no cross-sectional result admissible. |
| **H1** | Cross-sectional Wikipedia attention (log daily pageviews, 7d mean, z-scored daily) predicts forward 7-day return. | Mean Spearman rank IC \|IC\| < 0.02 with NW t < 2.0, **or** costed top-minus-bottom spread ≤ 0 net of 30 bps. |
| **H2** | The same measure predicts forward 7-day realized volatility. | Adds < 0.02 out-of-sample R² over a lagged-vol-only baseline (log rv_7 + log rv_30). |
| **H3** | Attention adds discrimination to the cross-sectional 7d/−20% drawdown flag. | OOS AUC ≤ 0.627 (`ml-forecast.md`'s measured 0.617 + 0.010). |

Free parameters: 2 (30-day attention baseline, 7-day horizon). Regimes: BTC above/below its
200d MA, and high/low trailing volatility. Costs: 30 bps, on throughout.

**Trials this run adds to the counter: 12.** Three targets × three samples (BTC time series,
ETH time series, 22-name cross-section), plus the Fear & Greed return and volatility tests,
plus the drawdown AUC. The threshold sweeps in §4 are reported as plateaus, not as separate
trials, per the lab's step 7. Against ~8,144 cumulative trials the deflated hurdle stays at
**≈2.32**; 12 more trials moves it in the fourth decimal. Nothing measured here came within a
factor of four of it.

---

## 2. research-scout: the ledger, and the source gate

`.claude/skills/research-scout/references/rejected-ledger.md` has **no** entry for sentiment,
attention, Google Trends, Reddit, Wikipedia or news text. So the idea is `verdict=open` — not
encouragement, permission to state a falsifier. But the ledger's neighbouring entries set the
prior precisely, and two of them apply almost word for word:

- **`onchain-flows`** — rejected because "none of it can be backtested". The same test is the
  one that kills Google Trends below.
- **`taker-imbalance`** — rejected because the sort "is re-sorting on past returns", with a
  contemporaneous correlation of 0.414 against the same bar's return. Measured here:
  attention's contemporaneous correlation with the **past** 7-day absolute return is
  **+0.238** on BTC and +0.146 on ETH, and the Fear & Greed index's with the past 7-day return
  is **+0.428**. Attention follows price. It is, in the ledger's phrase, a chart of where price
  has been.
- **`etf-flow-proxy`** — rejected because "a validated *source* is not a validated *signal*".
  Wikipedia pageviews is the cleanest source in this whole report and is still a dead signal.

### 2.1 The four source gates, applied

`references/source-test.md`: free, keyless, ≥730 days of history, lag below the decision
cadence, and point-in-time. All four, or out. Every row below was verified in this run, not
recalled.

| candidate | free | keyless | history | lag | point-in-time | **verdict** |
|---|---|---|---|---|---|---|
| **Wikimedia pageviews REST** | yes | yes | **4,105 days** (daily from 2015-07-01; legacy pagecounts to 2007) | ~1 day publication | **endpoint yes, series NO** — see §2.2 | **observe-only, and dead as a signal (§3–§5)** |
| **Google Trends** (`/trends/api/explore`) | yes | yes | ~2004 nominally | hours | **NO** | **rejected** |
| **Fear & Greed** (`api.alternative.me/fng`) | yes | yes | **3,160 days**, 2018-02-01 → 2026-09-30 | <1 day | **NO** (see §6) | **rejected** |
| **Reddit / Pushshift** | no | no | — | — | — | **rejected** |
| **GitHub `/stats/commit_activity`** | yes | yes | **52 weeks** (verified: the array has exactly 52 entries) | minutes | yes | **rejected — 364 days against a 730-day floor** |
| **GH Archive** (`gharchive.org`) | yes | yes | 2011 → | 1 hour | yes | **rejected on proportionality** — >18 GB of JSON *per day* decompressed, >500 GB for 2021 alone, against a laptop holding 28 Wh of a 95 Wh design |
| **Earn's own RSS news archive** | yes | yes | **1–2 days in the live feeds** (§7) | 15 min | forward-only | **observe-only; no historical backtest is possible today** |

**Google Trends, the specific failures.** There is no official API. The unauthenticated
widget endpoint returned **HTTP 429 Too Many Requests on the first request from this host** in
this run. And even if it answered, the series is disqualified by construction, not by access:
every Trends series is **normalized to its own window's maximum**, so the same query over two
different windows returns two different scales and the numbers cannot be concatenated; and the
values are drawn from a **sample** of searches, so successive pulls of the identical query
return different numbers, with the discrepancy largest exactly where Earn would need it — low-
volume terms. A series whose value for a past day changes when you ask again is not
point-in-time and cannot be backtested. The anchor-keyword trick that the SEO literature
recommends stitches scales together but does nothing about the sampling noise, and it adds a
hand-chosen anchor as a free parameter. **Rejected by rule, not by preference.**

**Reddit, the specific failure.** Pushshift was revoked from public access on **2 May 2023**
and is now restricted to verified moderators for moderation use only. The surviving mirrors
(Arctic Shift, PullPush) and the academic torrent dumps are third-party re-hosts of a dataset
whose collection stopped; they are not an endpoint an automated run can re-pull, and the
torrents fail the proportionality test for the same reason GH Archive does.
`needs_key`/`needs_application` is a rejection, not a caveat.

### 2.2 Why Wikipedia pageviews is a clean endpoint and a broken series

The API passes the four gates. The **article** does not. Verified in this run against
`en.wikipedia.org/w/api.php?action=query&redirects=1`:

| title asked for | exists today | redirects to | consequence |
|---|---|---|---|
| `Binance_Coin` | yes | **`Binance`** | BNB's attention is folded into the exchange's article |
| `BNB_(cryptocurrency)` | **missing** | — | the obvious title for the coin Earn buys for the fee discount does not exist |
| `Worldcoin` | yes | **`World (blockchain)`** | renamed; the series under the new title is **676 days** while WLD has been listed **1,158** days |
| `Matic_Network` | yes | **`Polygon (blockchain)`** | renamed mid-life |
| `Fetch.ai` | **missing** | — | FET has no article at that title |
| `Artificial_Superintelligence_Alliance` | **missing** | — | the merged entity FET became has no article |
| `Terra_(blockchain)` | yes | — | survives, but describes a dead chain |

Pageviews are keyed on the article title **as it is today**. When an article is renamed, the
series you can retrieve today starts at the rename, so **42% of WLD's listed life has no
attention data and the gap is invisible** — the API returns a shorter array, not an error. That
is a silent point-in-time violation of exactly the kind the scout's trap list exists to catch.
It cannot be fixed by a hand-maintained rename map, because a rename map assembled in 2026 is
a 2026 artefact applied to 2021 data.

**Hand-mapping the coins Earn can actually trade.** Of the 10 bases the config permits an
entry in today (`universe.core` BTC, ETH plus the 8 satellites that pass
`satellite_eligibility`: LINK, AAVE, LTC, XLM, WLD, ADA, SUI, AVAX), a hand-built map
resolved 9. **SUI has no en.wikipedia article at all** (`Sui_(blockchain)` returns 1 day), and
WLD's is truncated as above. Across the wider 22-name quality set, **6 of 22 hand-guessed
titles do not exist**: BNB, SUI, PEPE, NEAR, FET, HBAR.

### 2.3 Can the mapping be automated? No.

Measured: CoinGecko's free `/coins/list` (21,731 entries, keyless) → Wikipedia search → pageviews.

| layer | still-TRADING symbols | delisted symbols |
|---|---|---|
| has a CoinGecko entry at all | 470 / 482 (**98%**) | 160 / 197 (**81%**) |
| ticker maps to **exactly one** CoinGecko coin | 299 / 482 (**62%**) | 100 / 197 (**51%**) |
| (top-100 by volume) resolves to a Wikipedia title | 72 / 100 | 57 / 100 |
| (top-100) that title has a >400-day pageview series | **67 / 100** | **53 / 100** |

On the pre-registered metric, H0's falsifier does **not** trigger: delisted coverage (53%) is
79% of surviving coverage (67%), not below half. **But the metric is worthless, because the
resolutions are wrong.** What the automated pipeline actually produced for the highest-volume
symbols:

| symbol | CoinGecko name it picked | Wikipedia article it resolved to | days |
|---|---|---|---|
| BTCUSDT | `batcat` | — | 0 |
| ETHUSDT | `Anubis Bridged ETH (Anubis)` | — | 0 |
| SOLUSDT | `Allbridge Bridged SOL (Near Protocol)` | — | 0 |
| XRPUSDT | `Binance-Peg XRP` | `USDC_(cryptocurrency)` | 498 |
| ADAUSDT | `TOLY'S DOG` | `Killing_of_Renée_Good` | 261 |
| NEARUSDT | `Binance-Peg NEAR Protocol` | `Cryptocurrency` | 4,105 |
| UNIUSDT | `Linea Bridged UNI (Linea)` | `Formula_One_sponsorship_liveries` | 4,105 |
| MATICUSDT (delisted) | `MATIC (migrated to POL)` | `Peter_Thiel` | 4,105 |
| TAOUSDT | `Bittensor` | `Digital_Currency_Group` | 3,790 |
| DOGEUSDT | `Binance-Peg Dogecoin` | `Binance` | 3,117 |

Bitcoin resolves to *batcat*. Cardano resolves to a murder victim. The nominal 67% coverage
is nominal only: the symbol→name layer is ambiguous for 38% of live tickers and picks a
wrapped/bridged/meme impostor when it is not, and the name→article layer then compounds it.
**A defensible ticker→article map must be hand-built, which makes it a tier-2 human artefact
built with today's knowledge — i.e. survivorship-contaminated by construction.** That is the
real blocker, and it is harder than the one H0 pre-registered.

**H0 verdict: `inconclusive` on the pre-registered metric; the study instead establishes a
stronger blocker (correctness of resolution, and title instability) that no amount of coverage
fixes.** Everything from §3 on was therefore run on the *best case* — hand-mapped titles,
survivors only, bias deliberately in attention's favour. A null there is a strong null.

---

## 3. H1 and H2 on BTC and ETH: the clean, high-powered test

Earn trades BTC and ETH only, and `Bitcoin`/`Ethereum` are the two most stable, deepest
articles in the encyclopaedia. This is the most favourable possible test of the idea.

Measure: `att_t = log(views_t−1) − mean(log(views) over t−31..t−2)` — abnormal attention with
an explicit **one-day publication lag**, because Wikimedia publishes day *t* after day *t*
closes. Sample 2017-09-16 → 2026-09-17, **n = 3,289 daily observations each**.

### 3.1 The trap check, first

| | BTC | ETH |
|---|---|---|
| corr(att, past 7d return) | **+0.128** | +0.137 |
| corr(att, \|past 7d return\|) | **+0.238** | +0.146 |
| corr(att, trailing 7d realized vol) | +0.082 | +0.045 |

Attention rises *after* price moves, in either direction. This is the `taker-imbalance`
signature and it is why any quintile sort on attention will look plausible and mean nothing.

### 3.2 H1 — forward return

| | beta | NW(7) t | Spearman | n |
|---|---|---|---|---|
| BTC forward 7d return ~ att | +0.01359 | **+0.82** | **−0.0162** | 3,289 |
| BTC forward 1d return ~ att | +0.00142 | +0.48 | — | 3,289 |
| ETH forward 7d return ~ att | +0.00153 | **+0.09** | +0.0057 | 3,289 |
| ETH forward 1d return ~ att | +0.00328 | +1.15 | — | 3,289 |

**Falsifier triggered on both legs**: \|Spearman\| is 0.0162 and 0.0057, both under 0.02, and
every t is under 2.0.

### 3.3 The decile table, both directions, BTC

| attention decile | n | median fwd 7d | mean fwd 7d | median fwd 7d RV | median fwd 7d max drawdown | P(7d dd < −8%) |
|---|---|---|---|---|---|---|
| 0 (lowest) | 329 | +0.15% | −0.06% | **0.600** | −3.52% | **23.4%** |
| 1 | 329 | +0.67% | +0.71% | 0.462 | −2.17% | 13.4% |
| 2 | 329 | +0.76% | +1.24% | 0.457 | −2.47% | 13.7% |
| 3 | 329 | **+1.36%** | +1.63% | **0.416** | −1.72% | **9.7%** |
| 4 | 329 | +0.94% | +1.16% | 0.456 | −2.00% | 15.2% |
| 5 | 328 | +0.41% | +0.61% | 0.425 | −2.65% | 16.8% |
| 6 | 329 | −0.41% | −0.86% | 0.473 | −2.89% | 21.3% |
| 7 | 329 | +0.48% | +0.46% | 0.519 | −2.73% | 18.5% |
| 8 | 329 | −0.17% | +0.03% | 0.579 | −2.94% | 25.2% |
| 9 (highest) | 329 | +0.77% | +1.67% | **0.626** | −2.99% | 23.4% |

Return is non-monotone noise: the best median is decile 3 and the mean is highest in decile 9.
Volatility and drawdown are **U-shaped** — both the quietest and the loudest attention days
carry more forward volatility than the middle. A U-shape is not a tradable ordering, and it is
what you get when a variable is a noisy function of recent absolute price change, which §3.1
says it is. The ETH table has the same shape (deciles 0 and 8–9 at RV 0.74–0.75 against
0.60–0.65 in the middle).

### 3.4 H2 — forward volatility, and whether it adds anything

In-sample the volatility link is real and weakly positive:

| | beta | NW(7) t | Spearman |
|---|---|---|---|
| BTC log fwd 7d RV ~ att | +0.1784 | **+2.02** | +0.0724 |
| ETH log fwd 7d RV ~ att | +0.1043 | +1.70 | +0.0270 |

That is the one number in this report that looks like something, and it is exactly why the
pre-registered falsifier was written as *incremental* out-of-sample R², not a t-statistic.
Walk-forward, expanding window, weekly non-overlapping observations, 282 out-of-sample weeks:

| asset | OOS R², lagged vol only | OOS R², + attention | **Δ** | falsifier bar |
|---|---|---|---|---|
| BTC | 0.1734 | 0.1687 | **−0.0047** | +0.020 |
| ETH | 0.1426 | 0.1578 | **+0.0152** | +0.020 |

**Falsifier triggered on both.** On BTC attention makes the forecast *worse*. On ETH it adds
less than the pre-registered bar. Note also that this weak baseline (0.17 / 0.14 OOS R² on
weekly log RV) is nothing like the shipped HAR+DVOL blend's **0.60 on BTC / 0.54 on ETH** —
those are a different target at a different horizon and the two are **not** comparable, so no
claim is made against them. The honest statement is narrower and worse for the hypothesis:
attention fails to add even to a deliberately weak trailing-vol baseline.

---

## 4. The costed rule, and both directions of it

Arithmetic Sharpe, daily rebalance, 30 bps round trip charged on every position change.
Buy-and-hold on the same window is the benchmark; the shipped strategy's 0.83 is over a
different window and is not restated here.

**BTC, buy and hold: Sharpe 0.510, mean +0.0937%/day.**

| rule | Sharpe | mean/day | time invested |
|---|---|---|---|
| LONG when att > q0.50 | −0.018 | −0.0025% | 50% |
| **FLAT when att > q0.50** | 0.286 | +0.0351% | 50% |
| LONG when att > q0.60 | −0.174 | −0.0223% | 40% |
| **FLAT when att > q0.60** | **0.413** | +0.0543% | 60% |
| LONG when att > q0.70 | 0.039 | +0.0043% | 30% |
| FLAT when att > q0.70 | 0.228 | +0.0337% | 70% |
| LONG when att > q0.80 | −0.049 | −0.0046% | 20% |
| FLAT when att > q0.80 | 0.361 | +0.0573% | 80% |

**ETH, buy and hold: Sharpe 0.301, mean +0.0714%/day.**

| rule | Sharpe | mean/day | time invested |
|---|---|---|---|
| LONG when att > q0.50 | 0.093 | +0.0154% | 50% |
| FLAT when att > q0.50 | −0.077 | −0.0131% | 50% |
| LONG when att > q0.60 | 0.039 | +0.0060% | 40% |
| FLAT when att > q0.60 | 0.007 | +0.0013% | 60% |
| LONG when att > q0.70 | 0.082 | +0.0110% | 30% |
| FLAT when att > q0.70 | 0.034 | +0.0067% | 70% |
| **LONG when att > q0.80** | **0.428** | +0.0474% | **20%** |
| FLAT when att > q0.80 | −0.068 | −0.0144% | 80% |

Read this the way the lab's step 7 requires — the plateau, not the peak. **On BTC every
"buy the attention" rule is at or below zero and every rule loses to buy-and-hold's 0.510.**
The one cell that beats its own buy-and-hold is ETH at q0.80, Sharpe 0.428 against 0.301 —
and it is the peak of an otherwise flat and sign-unstable surface (0.093, 0.039, 0.082,
0.428), it holds a position only 20% of the time, its BTC twin is **−0.049**, and the deflated
hurdle is **≈2.32**. It is search noise with a number attached. Reporting it is the point;
shipping it would be the `oi-deleveraging-buy` mistake.

**Both directions, stated.** The best-looking BTC rule is the *inverse* one — stay out when
attention is high — at Sharpe 0.413. What it gives up is the return: mean/day falls from
+0.0937% to +0.0543%, so it forfeits **42% of BTC's drift** to buy a Sharpe that is still
0.10 *below* simply holding. It dodges some volatility by also dodging the rallies, which is
precisely the shape step 8 exists to expose.

### 4.1 Regime split

NW(7) t-statistics within each regime, with each regime's n:

| regime | n | forward-return t | forward-vol t |
|---|---|---|---|
| BTC, above 200d MA | 1,625 | **−0.96** | +2.62 |
| BTC, below 200d MA | 1,664 | **+1.52** | +0.80 |
| BTC, high trailing vol | 1,644 | +1.66 | +0.84 |
| BTC, low trailing vol | 1,645 | **−2.03** | +1.61 |
| ETH, above 200d MA | 1,577 | +0.57 | +0.72 |
| ETH, below 200d MA | 1,712 | −0.42 | +1.79 |
| ETH, high trailing vol | 1,644 | +0.08 | +1.35 |
| ETH, low trailing vol | 1,645 | +0.08 | +0.94 |

The return sign **flips between regimes on BTC** (−0.96 in bull, +1.52 in bear; −2.03 in low
vol, +1.66 in high vol) and is flat on ETH. Sign instability of that size across a routine
regime cut is the definition of an artefact — the same reasoning that killed
`cross-sectional-momentum` in the ledger.

---

## 5. The cross-sectional test, on the best case

Hand-mapped titles, survivors only, 2021-01-01 → 2026-09-17, days with ≥8 names.
**16 names, 26,615 observations, 2,086 days.** Attention is z-scored across names each day.
IC significance uses a Bartlett-lag-7 HAC correction on the daily IC series, because the
7-day target overlaps.

| target | rank IC, **attention** | HAC t | rank IC, **trailing vol** (already free, already computed) | HAC t |
|---|---|---|---|---|
| forward 7d return | **−0.0005** | **−0.04** | −0.0735 | **−4.93** |
| forward 7d realized vol | **+0.0127** | **+1.00** | **+0.4636** | **+33.38** |
| forward 7d max drawdown | **−0.0048** | **−0.52** | −0.2281 | **−18.27** |

**H1 falsifier triggered** (\|IC\| 0.0005 ≪ 0.02, t = −0.04). **H2 falsifier triggered**
(IC 0.0127 < 0.02, t = 1.00, and the comparison is brutal: trailing volatility's IC against
the same target is **36× larger** at t = +33).

### 5.1 Quintiles of cross-sectional attention

| quintile | n | median fwd 7d | mean fwd 7d | median fwd 7d RV | median fwd 7d dd | P(7d dd < −20%) |
|---|---|---|---|---|---|---|
| 0 (lowest) | 5,323 | −0.30% | −0.03% | 0.677 | −4.06% | 6.89% |
| 1 | 5,323 | −0.25% | +0.24% | 0.653 | −3.92% | 6.48% |
| 2 | 5,323 | −0.34% | −0.09% | **0.635** | −3.93% | **5.82%** |
| 3 | 5,323 | −0.54% | −0.12% | 0.648 | −4.07% | 6.35% |
| 4 (highest) | 5,323 | −0.22% | +0.42% | **0.686** | −4.24% | 7.01% |

The same U-shape, and the same absence of an ordering. Every median forward 7-day return is
**negative**, in all five buckets — which is the `growth-audit` finding (median eligible
altcoin −7.65%/30d) showing up again, not an attention effect.

### 5.2 The costed books

| book | n | mean 7d gross | **net of 30 bps** |
|---|---|---|---|
| attention quintile 0 | 5,323 | −0.032% | −0.332% |
| attention quintile 1 | 5,323 | +0.239% | −0.061% |
| attention quintile 2 | 5,323 | −0.093% | −0.393% |
| attention quintile 3 | 5,323 | −0.122% | −0.422% |
| attention quintile 4 | 5,323 | +0.416% | **+0.116%** |
| **BTC hold, same window** | — | — | **+0.305%** |
| equal weight all 16 names | — | — | +0.082% (gross) |

The best attention quintile nets **+0.116% per 7 days against BTC hold's +0.305%** — it loses
to doing nothing, by a factor of 2.6, in a test rigged in its favour. Top-minus-bottom spread
is +0.448% gross per 7 days, which two round trips (0.60%) erase entirely.

### 5.3 H3 — the drawdown flag, and an unintended corroboration

The one unfinished ML thread is a cross-sectional 7d/−20% drawdown ordering at AUC 0.617
against a hand-set 0.543, top-decile lift 1.73. Tested here on the same 26,615 observations,
base rate 6.51%, 1,733 positives:

| score | AUC | top-decile lift |
|---|---|---|
| **attention z** | **0.502** | 1.25× |
| attention \|z\| (the U-shape, given its best chance) | 0.527 | — |
| **trailing volatility z, one free feature** | **0.609** | **1.78×** |

**H3 falsifier triggered decisively**: 0.502 against a 0.627 bar. Attention is a coin flip on
crypto's most important risk question.

The other row is the finding worth keeping. A **single trailing-volatility feature, computed
from candles this repo already stores, reproduces AUC 0.609 and top-decile lift 1.78× on an
independent 16-name panel** — against `ml-forecast.md`'s nine-model-family figure of 0.617 and
1.73×. That is an out-of-sample corroboration of the repo's one unfinished ML thread, arrived
at here by accident while testing something else, and it says the thread is real and cheap.
It belongs to Track 2/4, not here, but it should not be lost: **the risk ordering does not need
a model zoo, it needs wiring.**

---

## 6. The Fear & Greed index: 60% price arithmetic in a sentiment costume

Verified: `api.alternative.me/fng/?limit=0` is free, keyless, returns **3,160 daily entries
from 2018-02-01 to 2026-09-30**. So it passes free, keyless, depth and lag. It fails
point-in-time, and it fails on content.

**Its published construction:** Volatility 25%, Market Momentum/Volume 25%, Social Media
(Twitter) 15%, Surveys 15% — **"currently paused"** — Dominance 10%, Google Trends 10%.

Three fatal problems, in order of severity:

1. **60% of the weight (volatility + momentum/volume + dominance) is arithmetic on price and
   volume Earn already computes from its own candles, at higher frequency, with no vendor.**
   Measured: corr(F&G, BTC past 7d return) = **+0.428**. It is a lagging restatement of the
   chart.
2. **The construction changed inside the history.** Surveys were 15% and are now paused; the
   Reddit component is described as built but not live. A series whose formula changed
   mid-sample has a silent regime break at an undocumented date, and a backtest over it is a
   backtest of two different variables concatenated.
3. **10% of it is Google Trends**, which §2.1 rejects as non-reproducible. Any series with a
   Trends component inherits Trends' irreproducibility.

Measured anyway, on 3,147 joined daily BTC observations (2018-02-01 → 2026-09-17):

| | beta | NW(7) t |
|---|---|---|
| BTC forward 7d return ~ F&G/100 | +0.0298 | **+1.83** |
| BTC log forward 7d RV ~ F&G/100 | −0.0129 | **−0.12** |

| F&G quintile | n | median fwd 7d | mean fwd 7d | median fwd 7d RV |
|---|---|---|---|---|
| 0 (extreme fear) | 679 | +0.97% | +0.50% | 0.524 |
| 1 | 585 | −0.17% | −0.33% | 0.471 |
| 2 | 639 | −0.11% | −0.20% | **0.417** |
| 3 | 648 | +0.66% | +0.95% | 0.442 |
| 4 (extreme greed) | 596 | +0.76% | **+1.61%** | 0.540 |

Note the sign: the coefficient is **positive** — more greed, *higher* forward return
(t = +1.83, below 2.0, so not even that) — which is backwards from the folklore the index is
sold on, in exactly the way `funding-as-direction` is backwards in the ledger. And the table
is U-shaped again. **Rejected.** "Buy fear, sell greed" is not in this data.

---

## 7. Earn's own news archive: what could be rebuilt, and the answer is almost nothing

`config/earn.yaml: news.whitelist` has 9 feeds and a **two-source corroboration rule**
(`min_sources: 2`). Ingest runs every 15 minutes (`ops.schedules.ingest`), so the archive
accumulates **forward** from whenever ingest first ran. The question is whether history could
be rebuilt. Measured this run, live, without touching the database:

| feed | items in the live feed | oldest → newest item now in it |
|---|---|---|
| CoinDesk | 25 | 2026-09-29 10:13 → 2026-09-30 14:04 |
| Cointelegraph | 30 | 2026-09-29 05:29 → 2026-09-30 13:30 |
| TheBlock | 19 | 2026-09-28 13:39 → 2026-09-30 13:33 |
| Blockworks | 50 | 2025-12-02 → 2026-09-30 |
| Decrypt | 36 | 2026-09-28 10:16 → 2026-09-30 11:54 |
| BitcoinMagazine | 10 | 2026-09-29 13:32 → 2026-09-30 13:18 |
| EthereumFoundation | **640** | 2016-04-01 → 2024-01-31 |
| BitcoinCore | 112 | 2024-07 → 2026-01 |
| FederalReserve | 20 | (no parseable date field in the feed) |

**Six of the nine secondary feeds carry one to two days.** Only the two low-frequency *primary*
sources (Ethereum Foundation blog, Bitcoin Core) carry years, and those publish a handful of
posts a month — they can never satisfy a two-source corroboration rule on a market event.

Could the Internet Archive replay them? Partially, and not honestly:

| feed | distinct archived days in the Wayback CDX | span |
|---|---|---|
| CoinDesk | 1,086 | 2021-11-03 → 2026-09-29 |
| Cointelegraph | 2,058 | 2014-05-05 → 2026-09-12 |
| TheBlock | **0** | — |
| Blockworks | **0** | — |
| Decrypt | **0** | — |

Two of nine feeds are archived at all. Each archived snapshot holds ~25–30 items, i.e. roughly
the last 12 hours, and snapshots are irregular, so an unknown and **time-varying** fraction of
items is simply missing. The corroboration rule needs two independent sources on the same
event; with only CoinDesk and Cointelegraph replayable, from 2021-11 at best, with unquantified
per-day completeness, any "news backtest" built on it would have a selection bias whose sign
and size cannot be stated. **Verdict: `observe-only`.** The archive Earn is building forward is
genuinely valuable and will be backtestable in a few years. It is not backtestable today, and
no LLM news feature may be justified by a historical number.

---

## 8. Part one, closed

| hypothesis | falsifier triggered? | **outcome** |
|---|---|---|
| H0 — retrievable, survivorship-free attention series | not on the pre-registered metric; a **stronger** blocker found (wrong resolutions, unstable titles) | **inconclusive, and superseded** |
| H1 — attention predicts forward return | **yes**, on BTC, ETH and the cross-section | **REFUTED** |
| H2 — attention predicts forward volatility | **yes**, ΔR² −0.0047 / +0.0152 against a 0.020 bar | **REFUTED** |
| H3 — attention orders drawdown risk | **yes**, AUC 0.502 against a 0.627 bar | **REFUTED** |

The honest prior from the literature matched the result, and it is worth recording because it
is what the owner would have been told by anyone who had read it. The most careful study of
Google search volume and crypto — which built multi-annual consistent Trends series precisely
to avoid the normalization trap — found **returns not predictable and volatility partly
predictable**, and that the return result held at hourly and weekly frequency alike
(Journal of International Financial Markets/Int. Rev. Fin. Analysis line of work, 2019). The
positive Trends results in the literature are concentrated on **trading volume**, not return
(Financial Innovation, 2018: Bitcoin returns not predictable from Google searches, volume is).
The Wikipedia line is more encouraging for the **higher moments** — a 2023 *Risk Management*
paper finds Wikipedia attention predicts next-day realized **skewness** — which is consistent
with what §3.3 and §5.1 found: a U-shape in forward volatility and no ordering in forward
return. Skewness is not something a long-only spot book at full exposure can trade
(`dip-strategy.md` s0.2: every signal is expressible only as a reduction from fully invested).

**Recommendation for the ledger** (a human adds it; this run cannot):

> **`attention-sentiment`** — Google Trends, Reddit, Fear & Greed, GitHub activity and
> Wikipedia pageviews as return, volatility or risk inputs. Trends is sampled and
> window-normalized, so the same query returns different numbers and cannot be backtested
> (and returned HTTP 429 on the first unauthenticated call). Pushshift was revoked 2023-05-02.
> GitHub `/stats/commit_activity` returns 52 weeks against a 730-day floor. Fear & Greed has
> 3,160 days but 60% of its weight is price/volume arithmetic (corr +0.428 with the past week's
> return), its survey component was silently paused mid-history, and 10% of it is Trends.
> Wikipedia pageviews is the only clean endpoint (4,105 days, keyless) and is broken as a
> series: titles rename (`Worldcoin` → `World (blockchain)` truncates WLD's series to 676 of
> 1,158 listed days) and 6 of 22 quality-set coins — including BNB and SUI — have no article.
> Measured on the best case (hand-mapped, survivors only): forward-return NW(7) t = +0.82 (BTC)
> / +0.09 (ETH), cross-sectional rank IC −0.0005 at t = −0.04; incremental OOS R² for forward
> vol −0.0047 (BTC) / +0.0152 (ETH) against a 0.020 bar; drawdown-flag AUC 0.502 against
> trailing volatility's 0.609. Attention follows price: corr with the prior week's absolute
> return +0.238.

---

## 9. Part two — what else the LLM could do, ranked by whether code can re-check it

Earn's discipline is the one that matters here: **a model may only produce something host code
re-checks.** Schemas, cited feature keys, the two-source news rule, `proposal_loader.py`
re-checking `schemas/proposal.py` structurally in-container. Everything below is graded on that
and nothing else. Costs use the real budget structure in `config/models.yaml`: a **$150/month
total cap**, `classify` at $5, `extract` at $4, `flags` at $4, `holdings_watch` at $4,
`scan` at $3, `brief` at $12, `validate` at $30, `decide` at $60, `review` at $70,
`daily_review` at $40.

### 9.1 The ranking

| # | idea | what the model produces | **what re-checks it, deterministically** | verifiability | est. $/month |
|---|---|---|---|---|---|
| **1** | **Exchange notice → structured venue fact** | `{symbol, event ∈ {delist, halt, maintenance, deposit_suspend}, effective_at, source_url}` | `GET /api/v3/exchangeInfo` — the claimed symbol's `status` and `isSpotTradingAllowed` either corroborate it or the fact is discarded. The notice URL must be on the whitelist. Two-source rule applies. | **HIGH — the fact is a prediction about an API field code can read** | +$2 on `classify` |
| **2** | **News severity class for the existing fast path** | one of `{critical, material, noise}` + the `news_hash` it cites | the item must exist in the archive with ≥2 sources (already enforced); severity only *reorders* an existing queue and can never create an entry. Graded nightly against the realized move by `daily_review`, so the classifier accrues a measurable hit rate. | **HIGH — bounded output, cited evidence, scoreable** | inside `classify`'s $5 |
| **3** | **Plain-words explanation of a gate refusal, on the console** | one paragraph per refusal | the refusal itself is already a structured `{check_id, limit, observed}` from `riskgate.py`. The explanation must cite an existing `check_id` and the config key its limit came from, or it is not rendered. It **cannot change the decision** — it is rendered after the fact. | **HIGH — it explains a record it cannot alter** | ~$3 on `extract` (cache per check_id; most refusals repeat) |
| **4** | **Post-mortem of a losing trade against its own evidence pack** | root cause + a `fix_path` from a closed enum, citing `journal/snapshots/signals/<id>/pack.json` | every claim must cite a feature key in `runs/features: REGISTRY` and a value present in the pack; `post-mortem` already owns the grading split and `lessons.md`. A claim citing an unregistered key is rejected. | **HIGH — the pack is frozen, so every claim is checkable against it** | already inside `daily_review`'s $40 |
| **5** | **Drafting hypotheses with falsifiers for hypothesis-lab** | a pre-registration block | `hypothesis.py open` refuses >4 free parameters, a missing falsifier, a falsifier not checkable from the same data, or <2 regimes; the seal then makes post-hoc editing detectable; `evals/verify_change.py` recomputes every number before a merge. | **HIGH — this is the one place the guard rail already exists and is load-bearing** | already inside `discover`'s $35 |
| **6** | **Reconciling a broken invariant** | a diagnosis and a proposed patch | the invariant is re-asserted by test after the patch; a `skill_new` touching `scripts/**` is always held for a human; `runs/apply_changes.py` refuses any commit touching a tier-2 path. | **MEDIUM — the check exists but the diagnosis is not itself verifiable, only its fix** | $0 marginal |
| **7** | Attention/sentiment score from social text | a number | **nothing** — there is no point-in-time history to score it against (§7), so a hit rate cannot be computed for years | **LOW — reject** | — |
| **8** | Price or direction forecast from news text | a view | `ml-forecast.md`: direction is not forecastable at a size that survives 15 bps. Adding text does not change the sample-size fact. | **NONE — reject** | — |

### 9.2 Why #1 is the one to build, with the number attached

The task brief names the USDT peg and a venue halt as an **uncovered scenario**, and that is
confirmed by reading the code: `strategies/riskgate.py` contains no `peg`, `depeg`, `halt` or
venue-status check, and the only venue references in `ops/` are about which Binance endpoint a
sleeve may reach. The `venue-guard` skill exists and is **not wired to anything the gate
reads** — the gate reads `knowledge/flags.json` and a blackout window.

What that costs, measured on the 195 delisted pairs in the panel with enough history:

| window before the pair's last print | median | mean | share negative |
|---|---|---|---|
| final 30 days | **−47.3%** | −56.8% | **83%** |
| final 7 days | **−18.5%** | −34.9% | **74%** |
| final 1 day | −1.5% | −6.5% | 62% |
| all-time panel high → last print | **−97.6%** | — | — |

(Simple returns, converted from the log figures −64.0% / −20.4% / −1.5% / −373.5%.)

Worst final weeks: BETAUSDT −95.1%, VIDTUSDT −94.7%, TROYUSDT −88.1%, AMBUSDT −86.6%,
WTCUSDT −86.4%, VIBUSDT −85.6%, WRXUSDT −83.1%, HARDUSDT −82.4%. And the delisted set is not
all dust: **MATIC, FTM, MKR, EOS, XMR, TON, AGIX, HNT, BTT** all left the panel. A coin Earn
watches can be delisted, and the decay starts about a month out, not on the day.

**The design that fits the discipline.** The LLM reads the Binance announcement page and
emits `{symbol, event, effective_at, source_url}` and nothing else — no score, no direction,
no weight. Host code then does the only thing that matters: it calls
`GET /api/v3/exchangeInfo` and checks whether that symbol's `status` is still `TRADING` and
`isSpotTradingAllowed` is still true. If the notice says delist and the API agrees, the fact
is written as a blackout flag through `ops.lib.flags` and the symbol is rendered `exit_only`
by the existing mechanism — the same path `universe.satellite_eligibility` already uses for a
satellite that fails its filter. If the API disagrees, the fact is discarded and journaled as
a classifier miss. **The model's output is a falsifiable claim about a field code can read,
which is the strongest form of verifiability available in this system.** Cost: about $2/month
on top of `classify`'s existing $5.

### 9.3 The USDT peg needs no LLM at all, and that is the point

Earn can read its own peg gauge off Binance spot, from data it already ingests. Measured from
the static panel:

| pair | n days | window | median dev from 1.0 | p01 | p99 | worst intraday low | days \|dev\|>50 bps | >100 bps |
|---|---|---|---|---|---|---|---|---|
| **USDCUSDT** | 2,676 | 2018-12-15 → 2026-09-24 | −0.01% | −0.69% | +1.03% | −80.0% (2021-12-04, a wick) | **114** | 39 |
| FDUSDUSDT | 1,157 | 2023-07-26 → 2026-09-24 | −0.09% | −0.31% | +0.35% | −12.7% (2025-04-02) | **4** | 1 |
| TUSDUSDT | 2,874 | 2018-05-31 → 2026-09-24 | −0.05% | −1.34% | +2.43% | −53.5% (2021-01-29) | 271 | 143 |
| BUSDUSDT | 1,548 | 2019-09-20 → 2023-12-15 | −0.02% | −0.30% | +0.22% | −99.9% (first print) | 3 | 0 |

USDCUSDT days more than 50 bps off parity, **by year**: 2018 → 11, 2019 → 98, **2020 → 1,
2021 → 0, 2022 → 1, 2023 → 3, 2024–2026 → 0.** The 2018–19 cluster is USDC's own early
illiquidity, not USDT stress. Since 2020 the gauge has fired **five times in seven years**.
That is exactly the profile a guard should have: deterministic, free, and almost never
triggering.

Two design notes that the data forces:

- **Use the close, or the median of USDC/FDUSD/TUSD — never the low.** The −80% "low" on
  USDCUSDT in 2021 and the −99.9% on BUSDUSDT's first print are wicks and listing artefacts. A
  check on `low` would fire on data noise and a check on the median of three stables would not.
- **Do not turn the peg into a signal.** BTC's forward 7-day median return on the 114 days
  USDC/USDT was >50 bps off parity is **+2.70%** against +0.40% otherwise. That looks like an
  edge and it is not: 98 of those 114 days are in 2019, so the comparison is a comparison of
  eras, not of states. It is reported here only so nobody rediscovers it and sells it. The peg
  belongs in the gate as a **refusal**, never in a prompt as a **view**.

### 9.4 Cadence — how often the LLM side actually looks, from the committed config

The owner asked how frequently things are checked. On the model-facing side,
`config/earn.yaml: ops.schedules` and `watch` say:

| job | cadence | what it is |
|---|---|---|
| `scanner` | **every 5 minutes** | deterministic detectors (`volume_spike` z≥3.0 on 1h, `move` 2.5%/1h, `near_stop`, `breakout`, `rsi_extreme`, `dip_from_high`) — no model call unless a detector fires |
| `healthcheck` | every 5 minutes | liveness, mode match, kill switch |
| `watch` | **every 7 minutes** | the holdings watcher — one fuzzy question per holding, on the **local** model only (`local_only: true`, ≤8 holdings/cycle) |
| `ingest` | **every 15 minutes** | RSS pull + classify (news window 12h, `min_sources: 2`) |
| `nav_tick`, `reconcile` | every 15 minutes | — |
| `research_run` | at the configured Gulf slots | the `decide` chain, `min_tier: 4`, `allow_local: false` |
| `daily_review` | 21:30 Gulf, nightly | grades the previous day, process and outcome separately |
| `discovery_light` | 02:20 Gulf, nightly | 1 hypothesis, 2 folds, no proposal |
| `discovery_deep` | 04:00 Gulf, Saturday | 2 hypotheses, 4 folds, may package **one** survivor |
| `review_run` | 20:00 Gulf, Sunday | the weekly review |

So the *detectors* are 5-minutely and the *model* is invoked on escalation, at the research
slots, nightly and weekly. Nothing in this track argues for looking more often. Adding #1 and
#2 above adds no new cadence at all — both ride the existing 15-minute ingest.

---

## 10. Honest limits of this track

- **No live journal data was used.** Whether the news classifier's current hit rate justifies
  #2 above cannot be measured from static files; the design note says it *becomes* measurable
  once `daily_review` grades it, which is the honest version.
- **The cross-section is 16 names and hand-mapped by me, today.** Its power is low and its
  sample is survivor-only. Both biases run *in favour* of the hypothesis, which is why a null
  is reportable — but a positive result on this panel would not have been.
- **`hypothesis.py open`/`close` were not run**; the seal was reproduced by hand in the
  scratchpad because the script writes to a tier-2 path under the live state root. A human
  re-running the script against this pre-registration would get the same seal.
- **`scout.py check`/`gate` were not run** either — the repo mirror was still syncing over a
  slow mount while three sibling tracks copied the same tree. The ledger's prose page was read
  in full instead and the four gates were applied by hand with verified numbers. The
  machine-readable ledger remains the authority and contains no sentiment entry.
- **The Wayback completeness figure is a count of archived days, not of captured items.** The
  true per-day item completeness of a replayed feed is not measured here and would need a
  separate study; the conclusion (`observe-only`) does not depend on it, it is implied by it.
- **The 52-week GitHub figure is for one repo** (`bitcoin/bitcoin`). It is an API contract, not
  a per-repo property, but only one repo was checked.
- **F&G's 3,160 entries were counted from the `limit=0` response**; whether alternative.me
  has ever silently restated a past value cannot be tested from a single pull, and that is
  itself part of why it is rejected.
