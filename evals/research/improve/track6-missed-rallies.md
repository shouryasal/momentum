# Track 6 — The missed-rally forensics loop

**Status:** built and run, 2026-09-30. Workspace `im6`. Measurement only — nothing was
committed, no bot, cron, unit or console was touched, nothing was written into `~/earn-run`,
no order was placed, and **no live database was opened, copied or backed up at any point.**

**Code:** `evals/research/improve/missed_rally/{missed_rally,diagnostics,attention}.py`
**Raw outputs:** `evals/research/improve/missed_rally/out/*.json`, `out/rallies_attributed.csv.gz`
**Sealed pre-registration:** `out/PREREG.json`, seal `ffda34182aa214d8e160607a4dc83983`
at `2026-09-30T13:54:54Z`, re-checked at close and matching. **Close-out:** `out/CLOSE.json`

Every number is net of the standing cost floor — **0.30% a round trip** (10 bps fee + 5 bps
slippage per side), always on. Risk-adjusted return is the repo's **arithmetic Sharpe**
(mean/std × √365), never CAGR/vol.

---

## 0. Data discipline, because today's incident was caused by breaking it

The 2026-09-30 blocked-entry window (6h42m, `database is locked` on three healthchecks, no
`ingest_runs` rows 07:00:03Z → 13:41:56Z) coincided with heavy read-copies of the live
databases. This study therefore ran **entirely on static sources**:

| source | what it is | read how |
|---|---|---|
| `~/earn-panels/panel_1d.parquet` | survivorship-free daily panel, 747 USDT tickers that ever existed, 2017-08 → 2026-09-24 | read-only |
| `~/earn-run/data/binance/*.feather` | 324 candle files, never written mid-flight | read-only, one file opened to check its columns |
| `api.binance.com/api/v3/exchangeInfo` | the venue's current listing status and tick sizes | one unauthenticated GET, fetched once to `~/im6/ei.json` |
| `api.binance.com/api/v3/klines` | used once, on BTCUSDT, only to prove a field exists (§10.3) | one unauthenticated GET |

`knowledge/earn.db`, `journal/journal.db` and the `ft_userdata` sqlite files were **not
opened.** The system side of the forensics loop is therefore *reconstructed* by replaying the
shipped rules over the panel, and §11 gives the exact queries a live version would run
instead, with the columns it would need and the ones that do not exist yet.

---

## 1. The answer, on one screen

You asked why profitable coins were not bought, so that every miss becomes evidence instead
of a feeling. Here is the instrument, and here is what it found.

**Over the last 24 months (2024-09-25 → 2026-09-24) there were 2,605 rallies of +30% or more
inside 20 days, spread across 436 of the 493 currently-listed USDT pairs.** Rallies are not
rare. They are the normal behaviour of this asset class.

**Of those 2,605 rallies, 2,548 — 97.8% — never reached a coin Earn is allowed to buy.** Not
"a detector missed it", not "the gate refused it": the coin was outside the tradeable
universe before any signal, any model or any risk check was involved. The share is 97.7% at a
+20% bar and 97.6% at a +50% bar, so this is a property of the system, not of where I drew
the line.

**Only 57 rallies — 2.19% — got as far as a coin the sleeve could have entered.** Of those:
26 were in a coin below its own 200-day line, 23 happened while Bitcoin was below its own
200-day line (which puts the whole satellite sleeve in cash by design), 3 ranked below the
two available seats, 2 were **deleted by a sizing contradiction** (§9), 1 was owned and
exited before the peak, and **2 were captured.**

**And the misses are not worth what they look like.** Priced naively, one rally at a time, the
top three causes "cost" 1,055% of NAV per year — a number that is arithmetically impossible,
because it assumes holding 2,058 positions of 2.5% each at once in a book capped at 0.80×
gross. Priced honestly — take the shipped rule, relax exactly one cause, change nothing else,
no foresight — **relaxing them is worth between −0.47 and 0.00 percentage points over 24
months.** Zero to slightly negative.

**One thing, however, is genuinely broken, and it is a sizing contradiction rather than a
missed rally.** The shipped config asks for a satellite position of 2.5% of NAV
(`max_satellite_gross 0.05` ÷ `max_satellite_positions 2`); the book-level volatility target
then shrinks it to a **median 1.63%**; and the risk gate refuses to *open* anything below
`min_position_pct_nav 0.02`. So the system asks for a position it is then forbidden to take.
**On 292 of the 430 asset-days the book wanted a satellite — 67.9% — the position was deleted
by this.** Resolving the contradiction three different ways all lands in the same place:
+16.2% to +18.3% return and Sharpe 0.62–0.68, against the shipped +12.47% and 0.515, at the
same drawdown.

**The verdict, in one line:** the system is not missing rallies because of a detection gap.
It is missing them because the mandate deliberately excludes the coins they happen in, and
what little the mandate does allow is then deleted by a sizing rule that contradicts itself.
The mandate part is a choice already validated by earlier work; the sizing part is a defect,
and it is the single most common fixable cause.

---

## 2. What the instrument is

`evals/research/improve/missed_rally/missed_rally.py` takes a date range and produces one row
per rally, assigning each to exactly **one named cause** — the *first* gate it failed, the
same semantics `strategies/riskgate.py: CHECK_ORDER` uses when it names a refusal.

```
python missed_rally.py --start 2024-09-25 --end 2026-09-24 [--plateau]
python missed_rally.py --emit-live-queries     # prints the SQL a live version would run
python diagnostics.py                          # the squeeze, seat contention, threshold sweep
python attention.py                            # what the scanner can actually see
```

### 2.1 The rally rule, stated so it can be argued with

Walk each coin's daily closes forward. At day *t*, take the highest close in the next 20
days. If that is ≥ 1.30 × close[*t*] and no episode is open, an episode **starts** at *t* and
**peaks** at the argmax day; the next episode may not start until 20 days after that peak.
The dedupe is what stops one 90-day rally being counted as seventy overlapping 20-day ones.

Three free parameters, and the plateau is reported rather than a peak chosen:

| threshold | window | rallies found | mandate-cause share |
|---|---|---|---|
| +20% | 20d | 4,240 | **97.7%** |
| +30% | 10d | 2,189 | — |
| **+30%** | **20d** | **2,605** | **97.8%** |
| +30% | 60d | 2,475 | — |
| +50% | 20d | 1,186 | **97.6%** |

Median rally: **+34.2%, peaking 19 days after it starts.**

### 2.2 The named causes

Ordered as the real pipeline orders them. `C06`–`C08` describe the *signal* pipeline, which
feeds SleeveB and the event-fired research run, and are reported separately (§10) because
**SleeveA — the sleeve that actually trades the rules — never consults a detector.** Getting
that wrong is the one correction this study had to make to itself; see §12.

| code | cause | how it is decided |
|---|---|---|
| `C01` | not listed long enough | < `min_listing_age_days` 180 of history at rally start |
| `C02` | not in the watchlist | failed the funnel; the failing filter is named |
| `C03` | in the watchlist, no tier | 90d median volume below the $5M satellite membership floor → gate cap 0 → check `tier` refuses |
| `C04` | tier `major`, structurally unreachable | `SleeveA._book_targets` offers seats to satellites only |
| `C05` | tier `satellite`, rendered `exit_only` | fails `universe.satellite_eligibility`; the failing leg is named |
| `C06`–`C08` | no detector fired / screener dropped it / validation expired | SleeveB path, reported as an independent column |
| `C09` | BTC regime gate off | BTC below its own 200d MA → satellite sleeve in cash |
| `C10` | the coin's own regime gate off | the coin below its own 200d MA at rally start |
| `C11` | below the seat cutoff | eligible, regime up, ranked below the 2 seats |
| `C12` | sized to zero | a seat, regime up, target below `min_position_pct_nav` |
| `C13` | the gate refused it | a named check out of the 27 |
| `C14` | owned, exited before the peak | held on day 0, weight fell to 0 before the peak |
| `C15` | captured | held through |
| `C99` | unattributable | the falsifier bucket — **0 of 2,605** |

---

## 3. Validation: the replay reproduces the live config

A replay that cannot reproduce today's snapshot proves nothing. Mine re-implements
`ops/universe.py`'s funnel (`FILTER_ORDER`, `classify`, `assign_tier`, `_trend_quality`,
`_percentile_ranks`) and `strategies/sleeve_common.py`'s `select_satellites` /
`core_satellite_targets` line for line, point-in-time, and on its last snapshot date
(2026-09-20) it produces:

| | replayed | shipped `riskgate.json` | source of the shipped figure |
|---|---|---|---|
| watchlist | **106** | 107 | profit-audit, 2026-09-30 |
| tradeable (core + major + satellite) | **31** | 31 | profit-audit, 2026-09-30 |
| enterable (2 core + 8 satellites) | **10** | 10 | `timing-and-rally.md` §0 table |

Tier split: 2 core, 6 major, 23 satellite, 75 watchlist-only. The 387 exclusions break down
as liquidity 280, weekend-volume 62, real-world-asset 18, pegged 15, listing-age 9,
tick-size 2, data-only 1.

That is an independent re-derivation landing within one name of the live config. Note the
brief's "16 enterable" counts pairs in `riskgate.json` before `satellite_eligibility` is
applied; the number the sleeve can actually *hold* is 10, and `timing-and-rally.md` §0 says
the same.

---

## 4. The distribution — the finding

**2,605 rallies, 2024-09-25 → 2026-09-24, 493 currently-listed USDT pairs with panel
history.** `C99_unattributable` is empty, so falsifier **F1 did not trigger** (bar: >10%).

| cause | n | share | median rally | naive forgone (24m, % NAV) | capacity-bounded ceiling (24m) |
|---|---|---|---|---|---|
| `C03` in the watchlist, no tier | **951** | **36.5%** | +34.2% | 953.6% | 112.7% |
| `C02` not in the watchlist | **668** | **25.6%** | +33.8% | 676.6% | 86.0% |
| `C01` not listed 180 days yet | **439** | **16.9%** | +34.8% | 480.6% | 130.4% |
| `C05` satellite, `exit_only` | **394** | **15.1%** | +34.9% | 381.0% | 72.4% |
| `C04` major, structurally unreachable | **96** | 3.7% | +33.7% | 90.3% | 39.0% |
| `C10` its own regime gate off | 26 | 1.0% | +33.9% | 25.5% | 12.7% |
| `C09` BTC regime gate off | 23 | 0.9% | +32.5% | 19.1% | 9.8% |
| `C11` below the seat cutoff | 3 | 0.1% | +30.4% | 2.8% | 2.1% |
| `C12` **sized to zero** | 2 | 0.1% | +33.9% | 1.7% | 1.7% |
| `C14` owned, exited before the peak | 1 | 0.0% | +37.6% | 0.9% | 0.9% |
| `C15` **captured** | 2 | 0.1% | +33.8% | — | — |
| `C99` unattributable | **0** | **0.0%** | — | — | — |

**Universe and mandate causes (`C01`–`C05`): 2,548 = 97.8%.**
**Everything downstream of the universe: 57 = 2.19%.**

Regime split, and it barely moves: with BTC above its 200d line, 1,249 rallies, top cause
`C03` at 38.8%. Below it, 1,356 rallies, top cause `C03` at 34.4%. The universe is the binding
constraint in both regimes.

### 4.1 Who the 96 structurally-unreachable majors were

`C04` is a code fact, not a return finding, and `timing-and-rally.md` §0 already flagged it:
`SleeveA._book_targets` builds candidates as *satellite tier only*, and `is_satellite()` is
`tiers[a] == "satellite"`, so a **major** is neither core nor satellite and is never offered a
seat — whitelisted, capped at 0.15, priced, monitored, unbuyable. Over 24 months, 29 distinct
majors rallied 96 times: ZEC 8 times (median +49.2%), NEAR 7, ADA 7, AAVE 6, DOGE 6, SOL 6,
XRP 5, LINK 5, AVAX 5, LTC 4, CRV 4, SUI 3, HBAR 3, RUNE 3, PEPE 3, and 14 others.

Fixing it is measured in §8 (book **B2**) and is worth **nothing**: identical return, Sharpe
0.5225 vs 0.5147. It should still be fixed, because a config that lists six pairs the code
cannot reach is a lie about what the system does — but it is a correctness fix, not a return
fix, and it must not be sold as one.

---

## 5. The conditional table — the only place a fixable cause can be read

Because causes are ordered, a universe failure hides everything downstream. So here is the
table restricted to the 57 rallies that **did** reach a coin the sleeve could enter:

| cause | n | share of the 57 |
|---|---|---|
| `C10` the coin below its own 200d MA at the rally start | **26** | 45.6% |
| `C09` BTC below its own 200d MA → satellite sleeve in cash | **23** | 40.4% |
| `C11` ranked below the 2 seats | 3 | 5.3% |
| `C12` **sized to zero by the min_position floor** | 2 | 3.5% |
| `C15` captured | 2 | 3.5% |
| `C14` owned, exited before the peak | 1 | 1.8% |

Total forgone across all 57, at the shipped position size, using the shipped rule's own exit
rather than the peak: **4.58% of NAV over 24 months — 2.3% a year.**

**`C10` and `C09` together are 86% of this table, and neither is a defect.** A +30% rally that
begins while the coin is below its own 200-day line is precisely the trade the trend gate
exists to refuse, and `growth-audit.md` measured what removing that gate costs: an
equal-weight wide book drew down −96.6% against the gated core's −50.9%. Turning `C10` and
`C09` into buys is not an improvement; it is the construction the system was built to avoid.

The two `C12` rallies are named, dated and worth reading, because they are the defect:

| coin | rally start | gain | shipped score rank | seats held by | why not bought |
|---|---|---|---|---|---|
| CAKE | 2025-07-07 | +36.9% | **2** | BCH | target 0.0000 < min_position 0.02 |
| CAKE | 2025-09-04 | +30.9% | **1** | none | target 0.0000 < min_position 0.02 |

On 2025-09-04, CAKE was the **top-ranked** eligible satellite, its own regime was up, BTC's
regime was up, a seat was free — and the gate deleted the position because the rule asked for
less than 2% of NAV. Then it rallied 30.9%.

---

## 6. Three ways to price a miss, and only one of them is real

This is the part that matters most, and it is why "we missed a 300% rally" is almost always
the wrong sentence.

| how you price the top three causes | 24 months | per year | is it achievable? |
|---|---|---|---|
| **naive**, one rally at a time at 2.5% each | +2,111% of NAV | +1,055% | **No.** It implies 2,058 simultaneous positions of 2.5% = 51× gross, in a book capped at 0.80×. It is not a conservative estimate; it is not an estimate. |
| **capacity-bounded**, perfect foresight, 2 seats | +175.6% of NAV | +87.8% | **No.** Only 155 of 2,058 rallies physically fit two seats. It still requires knowing which 155. |
| **capacity-respecting, no foresight** — the shipped rule, one cause relaxed, nothing else changed | **−0.47pp to 0.00pp** | ≈ 0 | **Yes — this is the measurement.** §8. |

Across all 2,605 rallies, a perfect rally-picker at the shipped 2.5% weight fills 162 seats
and makes **+182.6% over 24 months.** The shipped book made +12.47%; buying and holding
Bitcoin made +32.98%. So the theoretical prize is large — and it is entirely a *selection*
prize, not a *universe* prize. Every measured attempt to win it is already on the rejection
ledger: cross-sectional momentum rank IC −0.016 to −0.069; a costed top-8 momentum rotation
at −13.5% CAGR and −98.9% drawdown, losing before costs; only 3.4% of coins beating BTC in
2023–24; 77.5% of +100% run-ups given back within 60 days.

**Falsifier F3 therefore split, and I am reporting it split rather than picking the
convenient side.** It triggers on both foresight-based numbers (>10%/yr) and does not trigger
on the only no-foresight measurement. I believe the no-foresight one, because the other two
describe a book that cannot exist.

---

## 7. What the sleeve actually held

Over 730 days the satellite sleeve — the half of the book that is supposed to catch rallies —
held a position on **138 asset-days out of 1,460 available seat-days: 9.5% utilisation, in 10
of 24 months.** It held nothing at all from December 2025 through September 2026.

Nine distinct names ever took a seat: BCH, CAKE, CKB, CRV, ENS, OG, SHIB, SUN, XLM.

| month | seats held |
|---|---|
| 2024-11 | SUN |
| 2024-12 | CKB, OG |
| 2025-01 | OG |
| 2025-02 | ENS |
| 2025-05 / 06 | BCH |
| 2025-07 | BCH, XLM |
| 2025-09 | CAKE, CRV, SHIB |
| 2025-10 | BCH, CAKE |
| 2025-11 | BCH |
| 2025-12 → 2026-09 | **nothing** |

Two things follow. The seat competition **does** bind — on 142 of the 392 risk-on days (36.2%)
there were more eligible, regime-up names than seats, up to 10 chasing 2. And the shipped
score is 40% liquidity percentile, 25% 365-day return, 35% trend quality, which is stable by
construction and, combined with three-rank rotation hysteresis, produced a nearly static
book: the sleeve's top two picks over two years included a fan token and Bitcoin Cash.

---

## 8. Counterfactual books — relax one cause, change nothing else

Each book is the shipped rule with exactly one thing widened. The score, the rotation
hysteresis, both regime gates, the book volatility target, the caps, the `min_position` floor
and the 0.30% round trip are unchanged, so the difference is attributable to the one rule that
moved. Weights are lagged one full day. **9 new selection trials, declared** (§13).

| book | what changed | 24m return | CAGR | Sharpe | max DD | asset-days held | asset-days deleted by `min_position` |
|---|---|---|---|---|---|---|---|
| **B0** | **the shipped rule** | **+12.47%** | +6.06% | **0.5147** | −11.88% | 138 | **292** |
| B1 | `satellite_eligibility` off (`C05`) | +12.00% | +5.84% | 0.5184 | −11.81% | 125 | 553 |
| B2 | majors get seats (`C04`) | +12.47% | +6.06% | 0.5225 | −11.76% | 110 | 622 |
| B3 | every watchlist name tradeable (`C03`) | +12.04% | +5.86% | 0.5079 | −11.82% | 105 | 616 |
| B4 | 6 seats at 15% gross | +11.21% | +5.46% | 0.4835 | −11.31% | 394 | 463 |
| B5 | whole watchlist, 6 seats, 15% gross | +11.80% | +5.74% | 0.5703 | −10.36% | 289 | 1,812 |
| **B7** | **1 satellite seat at 5% gross** | **+18.26%** | **+8.76%** | **0.6819** | −12.26% | 245 | **13** |
| **B8** | 2 seats at 10% gross | **+17.37%** | +8.35% | 0.6602 | −11.61% | 394 | 36 |
| **B9** | 2 seats at 5%, `min_position` floor removed | **+16.23%** | +7.82% | 0.6177 | −11.88% | 430 | **0** |
| — | **buy and hold Bitcoin** | **+32.98%** | — | **0.5424** | — | — | — |

Read the top block and the bottom block separately, because they say opposite things.

**Widening the universe does nothing, and the mechanism is visible in the last column.** B1,
B2, B3 and B5 all *reduce* asset-days held while multiplying the asset-days deleted by
`min_position` (292 → 553, 622, 616, 1,812). More candidates means more rotation, more
rotation means more *openings*, and every opening is where the 2% floor bites. Widening the
funnel feeds a bottleneck. That is why "trade more coins" measures at zero here, independently
of the profit-audit's finding that the 383 ignored pairs carry a median −3.26% CAGR.

**Resolving the sizing contradiction is the only thing that helps, and it helps three
different ways.** B7, B8 and B9 are three unrelated fixes — fewer seats, a bigger sleeve, no
floor — and they land at +16.2% to +18.3% and Sharpe 0.62 to 0.68, against +12.47% and 0.515,
at the same drawdown. **That spread is the plateau, and the plateau is the finding; B7's 0.682
is the peak and is not the claim.**

**And every book still loses to buying Bitcoin.** +18.26% against +32.98%. The best Sharpe
here, 0.682, is a long way below the deflated hurdle of **2.3225** (8,153 cumulative trials
over 9.1 years, baseline 0.83), and further still below **4.0137**, the hurdle on the 2-year
window these were actually measured on. **Nothing in this document is offered as an edge.**
B7/B8/B9 are offered as the repair of a config that contradicts itself.

---

## 9. The one fixable cause, with the arithmetic

`risk.max_satellite_gross` is 0.05 and `risk.max_satellite_positions` is 2, so
`core_satellite_targets` asks for **2.5% of NAV** per satellite. `book_realised_vol` then
scales the whole book to `vol.target_annual` 0.30, and because a satellite's own volatility is
typically 0.8–1.0 annualised against BTC's ~0.34, that scalar is usually well below 1. The
gate's `min_position` check refuses to *open* anything below `min_position_pct_nav` **0.02**.

Measured over the 430 asset-days the shipped book wanted a satellite:

| quantity | value |
|---|---|
| requested weight before volatility scaling | 0.0250 |
| **median weight after volatility scaling** | **0.01635** |
| mean / p10 / p90 / max | 0.01994 / 0.01048 / 0.03766 / 0.04737 |
| `min_position_pct_nav` | **0.0200** |
| volatility scalar needed to clear the floor | **0.80** (i.e. book vol ≤ 0.375) |
| **asset-days deleted by `min_position`** | **292 of 430 = 67.9%** |
| asset-days that survived | 138 |

Two thirds of the time, the system computes a position it is then forbidden to take. Each of
the three config values is defensible on its own — `max_satellite_gross 0.05` comes from
`dip-strategy.md` (0 of 6,720 costed satellite configurations beat holding BTC),
`min_position_pct_nav 0.02` from `wide-universe.md` §2.4 (at a $1,000 live seed a 2% position
is $20 and a half-trim is $10, under the $25 `min_notional`), and the 30% volatility target is
the shipped risk budget. **The defect is in their product, which nobody computed.**

The arithmetic says the cheapest consistent configuration needs
`max_satellite_gross / max_satellite_positions ≥ 0.02 / 0.654 = 0.0306`, where 0.654 is the
measured median scalar. At the shipped 5% gross that means **one seat, not two** — a single
config value, and the best-measured of the three fixes (B7).

This is a finding handed to the human, not a proposal. `config/**` is tier 2; the change would
go through the normal gate and be replayed by `evals/verify_change.py`. And it must be argued
on its own terms: one seat is *less* diversification, and `wide-universe.md` priced eight
positions at 1.82 of the 1.95 independent bets on offer. B8 (two seats, 10% gross) and B9
(floor removed) are the alternatives, measured, in §8.

---

## 10. Your other questions, answered from the config and the data

### 10.1 How often are we checking coins, and how often are we analysing?

From `config/earn.yaml: ops.schedules` and `signals.*`. Schedules are Gulf time.

| what | how often |
|---|---|
| candle ingest | **every 15 minutes** |
| detector scan across the watchlist | **every 5 minutes** (`signals.scanner.cron`) |
| holdings watcher (one fuzzy question per holding, local model) | **every 7 minutes** |
| NAV tick / exchange reconcile / healthcheck | every 15 / 15 / 5 minutes |
| **full Claude decision run** | **twice a day** — 08:30 and 16:00, plus event-fired runs |
| trading timeframe the sleeves act on | **4-hour candles** |
| universe resolver (which coins are even eligible) | **once a week**, Sunday 18:00 |
| nightly grading / weekly review | 21:30 daily / Sunday 20:00 |

And the throttles that decide how much of what the scanner finds can ever be acted on:
**5 candidates per 5-minute cycle**, a **240-minute dedupe** per detector/pair/direction,
**6 validations a day** with a 120-minute per-asset cooldown and a **90-minute expiry**, and
**3 research runs a day** with a 4-hour cooldown.

So: coins are *looked at* every 5 minutes; the system *decides* twice a day on 4-hour bars;
and eligibility is re-judged weekly. Given that the median rally here takes 19 days to peak,
the cadence is not the constraint. Nothing in the 2,605 rallies was missed for being too slow
— the smallest cause in the whole table is the one closest to timing.

### 10.2 Are we checking all the parameters — volume and the rest?

Not on most coins, and this is the sharpest thing I found outside §9.
`runs/signals/features.py` computes two tiers:

* **cheap tier — the whole watchlist:** `close`, `ret_24h`, `vol_ann_20d`, `ma200_dist_pct`,
  `dip_from_high_pct`, `news_count_24h`. **Six numbers.**
* **rich tier — `DEFAULT_RICH_PAIRS = 20` only:** the other ~18 keys, including `rsi_4h`,
  `vol_z_1h`, `range_high_20d`/`range_low_20d`, `ma_fast_1d`/`ma_slow_1d`.

The file says what follows: *"a detector reading a key a cheap pair does not have sees `None`
and does not fire"*. So of nine enabled detectors, only **`move` (24h)** and
**`dip_from_high`** can fire outside the rich 20. `breakout`, `volume_spike` and `rsi_extreme`
are structurally silent on every other name, and `ma_cross` is shipped `enabled: false`.

Which 20 get the rich tier is decided by `rank_watchlist`, an attention score of
`|ret_24h|·2.0 + max(dip,0)·0.5 + |ma200_dist|·0.1 + min(news,10)·1.5`. Replayed day by day
over the point-in-time watchlist (which ran ~220 names in the 2024-25 bull market, down to 106
by September 2026):

| | of 2,605 rallies |
|---|---|
| the coin was in the watchlist at rally start | **1,498 = 57.5%** |
| the coin was in the **rich 20** at any point in the 5 days before | **434 = 16.7%** |
| … given it was in the watchlist | 28.9% |
| a rich-only detector would have fired but the key was `None` | **478 = 18.4%** |
| at least one detector fired **in theory** (all keys available) | 2,434 = **93.4%** |
| at least one detector could fire **in practice** (tier split respected) | **90.1%** |

So the tier split costs 478 detector fires but only 3.3 points of *coverage*, because
`dip_from_high` fires on 2,257 of 2,605 rallies (86.6%) — which is also the problem with it:
a detector that fires before 87% of rallies is not selecting anything. `move_24h` fired on
1,056 (40.5%), `rsi_extreme` 316, `breakout` 235, `volume_spike` 188.

**The honest conclusion: detection is not the bottleneck.** The pipeline notices 90% of these
rallies. It cannot act on them because 97.8% are in coins outside the mandate, and the ones
inside it get deleted by §9. Enriching the feature tier would buy selectivity, not coverage,
and it should be argued for on that basis rather than on "we are missing rallies".

### 10.3 Recent *buy* volume specifically — no, and it is one free field away

You asked about "recent buy volume" as distinct from volume. **Nothing in Earn separates buyer
from seller.** `vol_z_1h` is a z-score of total volume; `qv` in the panel is total quote
volume; the feather store carries only `date, open, high, low, close, volume`.

Binance's public `/api/v3/klines` returns 12 fields per bar, and two of them are
`takerBuyBaseVolume` (index 9) and `takerBuyQuoteVolume` (index 10). Verified live today on
BTCUSDT: two consecutive 1-hour bars had taker-buy shares of **0.5329** and **0.4292** of
total volume. No key, same endpoint the ingest already calls, same rate-limit weight.

That makes "taker-buy share" and "buy/sell imbalance z-score" the cheapest genuinely new
features available to this system. I am **not** claiming they predict anything — that has to
go through `hypothesis-lab` with a pre-registered falsifier, both baselines and the deflated
hurdle, like everything else. I am saying the data is free, the field is named, and today it
is being discarded at ingest.

### 10.4 Internet search and timing the buy

There is no internet search, by invariant. `runs/decision_core.py` line 44:

```python
ALWAYS_DISALLOWED = ["WebFetch", "WebSearch", "Task", "Edit", "NotebookEdit"]
```

`ALWAYS_DISALLOWED` is one of the invariants no tier and no config can move. Claude cannot
Google anything inside Earn, ever. The only outside information is nine whitelisted RSS feeds
(CoinDesk, Cointelegraph, The Block, Blockworks, Decrypt, Bitcoin Magazine, the Ethereum
Foundation, Bitcoin Core, the Federal Reserve) ingested by code, classified into eight event
types, under a two-source corroboration rule.

That invariant blocks the *model* from fetching; it does not block the *system* from
ingesting. Widening the feed set, or adding a keyed social feed, is a code change in tier 2
and a legitimate thing to ask for. It is also worth knowing that the wide-universe study
measured news as an *attention* input (weight 1.5 in `rank_watchlist`), never as a direction —
and `audit-and-research-2026-09-29.md` refuted entry timing outright: of 12 hypotheses only
BNB fee payment (+0.215% of NAV/yr) and USDT cash yield survived, and both as arithmetic, not
alpha. Nothing in the 2,605 rallies here contradicts that: timing causes are the smallest
rows in the table.

### 10.5 What happened to ML, and what this study says about it

I did not re-run the ML work and I am not re-deriving it. `ml-forecast.md` is the record:
direction is not forecastable at a size that survives 15 bps (78 CPU + 9 GPU configs; the best
costed book *ties* BTC hold; a single feature, `−vol_60` cross-sectionally, beat the best model
at 4 of 6 targets). Volatility **is** forecastable, and the shipped HAR+DVOL blend already
reaches out-of-sample R² 0.60 on BTC and 0.54 on ETH against the ML zoo's best 0.24. "MAPE
near 1" is an anti-target — persistence alone is 0.44% on BTC 1h.

The one unfinished thread is a **cross-sectional risk ordering** (a 7-day, −20% drawdown flag)
at AUC 0.617 against a hand-set 0.543, top-decile lift 1.73, agreed by nine model families,
never wired into anything.

Track 6 has two things to say about where it belongs.

First, **not to satellite selection.** The satellite sleeve held a position on 9.5% of its
available seat-days and nothing at all for the last ten months. A ranker has almost nothing
to rank there, and §8 shows that widening its candidate pool makes the book worse until §9 is
fixed. Ordering a set the book cannot hold is not a use.

Second, **to the core and to the exit side, which is where the losses are.** The live evidence
is unambiguous: mechanical profit-takes 10 trades, +85.27, 10 of 10 won; the trend-loss
`exit_signal` 8 trades, −60.30, **0 of 8** won. `exit-and-horizon-2026-09-29.md` found all 78
fixed-hold books at or below buy-and-hold and the shipped 1h rule earning +0.022% per trade
against 0.300% of cost. A drawdown-probability ordering is an *exit and sizing* instrument
aimed at exactly the half the ensemble was never wired into — and it is the half that is 0 for
8 live. That is the argument for finishing it, and it is a different argument from the one
that got it built.

---

## 11. The live version: the queries, and the columns that do not exist

`python missed_rally.py --emit-live-queries` prints the whole set. Every table and column was
checked against `ops/sql/journal.sql` and `ops/sql/knowledge.sql` today. **None of it was
run.** A live version must read a snapshot handed over under the ops lock, never the live file
while a bot is trading.

| cause | table | the columns it needs |
|---|---|---|
| `C02`–`C05` | `state_snapshots` (knowledge) | `captured_at`, `payload_json` |
| `C06` | `signals` (journal) | `signal_id`, `ts_utc`, `detector`, `direction`, `detector_score`, `strength`, `status`, `status_reason`, `features_json` |
| `C07` | `signals` | `screen_score`, `screen_provider`, `screen_model`, `screen_rationale`, `status='screened_out'`, `dedupe_key` |
| `C08` | `signals` ⋈ `signal_validations` | `verdict`, `confidence`, `horizon_hours`, `escalated`, `error`, `outcome_ret`, `outcome_hit`, `pack_path` |
| `C08b` | `runs` | `stage`, `status IN ('missed','throttled','killed','failed')`, `error`, `signal_id`, `trigger_reason` |
| `C09`–`C12` | `proposals` | `module`, `targets_json`, `exposure_scale`, `abstain`, `valid`, `invalid_reason`, `consumed_status`, `consumed_reason`, `approval_status` |
| `C13` | `gate_decisions` | `reason` (the slug of the first failing check in `CHECK_ORDER`), `checks_json` (every check → pass/fail), `proposed_stake`, `action`, `severity` |
| `C12` | `gate_decisions` where `callback='custom_stake_amount'` | `action='clamp'` with `proposed_stake` under `min_notional_usdt` is the signature |
| `C14`/`C15` | `orders` ⋈ `fills`, then `gate_decisions` where `intent='exit'` | `fill_price`, `fill_amount`, `fee_amount`, `ft_trade_id`; `reason` on the exit row |
| cross-cutting | `incidents`, `risk_state`, `flags`, `ingest_runs` | `kind`/`severity`/`root_cause`; `locked_until`, `monthly_locked`; `expires_at`; `rows_written` |

Read with `ops.db.opened(path, readonly=True)` and `row_factory = sqlite3.Row` — never by
column position, because migrated and fresh databases differ in column *order*.

### Five columns a live version needs that do not exist today

1. **There is no record of what the scanner looked at and did not flag.** `signals` has a row
   per *candidate*. "No detector fired" is therefore an absence of evidence, not evidence, and
   cannot be told apart from a blind spot. **Needs:** a `scan_cycles` table
   (`scan_id`, `ts_utc`, `pairs_seen`, `candidates_found`, `candidates_kept`, `dropped_reason`).
2. **`screen_score` exists only for candidates that reached the screener.** A candidate cut by
   `max_candidates_per_cycle` leaves no trace. **Needs:** a row per candidate the cycle saw,
   including the cut ones, with the cut reason.
3. **The universe snapshot in force at an instant is only a dated file** under
   `knowledge/universe/`, not a DB row, so a point-in-time replay depends on a file surviving.
   **Needs:** a `universe_snapshots` table (`captured_at`, `pairs_json`, `tiers_json`,
   `scores_json`, `exit_only_json`, `excluded_json`).
4. **An entry that was never attempted leaves no `gate_decisions` row.** SleeveA's
   `instrument("want_none", …)` writes a log line. So §9's biggest number — 292 deleted
   asset-days — **is invisible in the journal today.** **Needs:** a `want_none` / `sized_zero`
   row (`ts_utc`, `sleeve`, `pair`, `reason`, `target_weight`).
5. **Nothing records which satellites held the seats.** `C11` has to be rebuilt from
   `nav_daily.positions_json`. **Needs:** `satellite_seats` (`ts_utc`, `sleeve`, `seat_index`,
   `asset`, `score`, `rank`).

Item 4 is the one that matters most. The single most common fixable cause this study found is
a cause the live system does not currently record at all.

---

## 12. Honest limits, and the two errors this study made

**Two bugs, both found by cross-checking rather than by luck, both fixed and both reported:**

1. `core_satellite_targets` was handed symbol keys (`BTCUSDT`) while reading base-asset
   weights (`BTC`), so the first replayed book held **zero** BTC and ETH and returned +0.48%
   instead of +12.47%. Caught because the attribution reported the BTC rally of 2024-10-22 as
   "sized to zero" while BTC was 1.06× above its own 200-day line. Every number in this
   document is post-fix.
2. The detector check originally sat *ahead* of the regime gates and mislabelled 8 of 10
   rallies whose real cause was the coin's own 200d gate. SleeveA never consults a detector;
   detector coverage is now an independent column, not a funnel step.

**Limits:**

* **No live journal data.** Per the standing rule, and stated in §11. `C07`, `C08` and `C13`
  are therefore *defined but not populated*: the screener's and validator's per-candidate
  decisions, and the gate's actual refusals, are reconstructed from the shipped rules rather
  than read. The attribution replays SleeveA faithfully and does **not** replay SleeveB — no
  LLM proposal, screen or validation verdict is in any of these numbers.
* **Daily resolution.** The panel is daily; the shipped sleeve trades 4-hour bars and the
  scanner runs every 5 minutes. `breakout` (tf 1d) and `dip_from_high` (tf 1d) are exact;
  `move`, `volume_spike` and `rsi_extreme` are modelled on daily analogues. Intraday rallies
  shorter than a day are invisible here.
* **Survivorship, measured rather than assumed.** Restricting to currently-listed pairs
  (as the brief specifies) drops **433 of 3,038 rallies = 14.3%**, in 108 coins that no longer
  trade. Those are rallies in things that later died, so including them would raise the
  mandate share, not lower it — the finding is conservative in the direction that matters.
* **`news_count_24h` is 0 offline**, which makes the replayed attention score *more* generous
  to price-movers than the live one. The 16.7% rich-20 hit rate is an upper bound.
* **`C06` is an absence of evidence** even in the replay: I compute whether a detector's
  *condition* held, not whether the live scanner had the candles to evaluate it.
* **The counterfactual books are not proposals.** They are the price tag on each cause. Two
  years is a short window: distinguishing Sharpe 0.68 from 0.51 at 80% power needs far more
  data than this, and `edge-audit`'s standing figure for 1.14 against 0.83 is about 245 years.
* **9 selection trials added** (B1–B9), taking the cumulative count from 8,144 to 8,153. The
  deflated hurdle is unchanged at **2.3225** over 9.1 years (baseline 0.83 +
  `expected_max_sharpe(8153, 9.1) = 1.4925`), and **4.0137** over the 2-year window these were
  measured on. Nothing here clears either. Nothing here is offered as an edge.

---

## 13. The verdict

**The system is bounded by its mandate, not by a detection gap — with exactly one real defect
inside the mandate, and that defect is the answer to your question.** Of 2,605 rallies of +30%
or more in 20 days across the 493 currently-listed USDT pairs over the last 24 months, 97.8%
occurred in coins Earn is not allowed to buy: too illiquid for a tier (36.5%), outside the
watchlist funnel (25.6%), listed for under 180 days (16.9%), or a satellite that fails the
eligibility filter and is rendered exit-only (15.1%). That exclusion is a deliberate choice
already validated by `growth-audit.md` and by the profit-audit's own census, and relaxing it
one cause at a time — with the shipped rule, no foresight, costs on — measures at between
−0.47 and 0.00 percentage points over 24 months, because widening the funnel feeds a
bottleneck rather than the book. Of the 57 rallies that did reach an enterable coin, 86% were
refused by a regime gate doing precisely the job it exists for. **The single most common
fixable cause is the sizing contradiction:** `max_satellite_gross 0.05` ÷
`max_satellite_positions 2` asks for 2.5% of NAV, the 30% book volatility target shrinks it to
a median 1.63%, and the gate's `min_position_pct_nav 0.02` then refuses to open it — deleting
the position on 292 of the 430 asset-days the book wanted one, 67.9%, including a CAKE rally
of +30.9% on 2025-09-04 when CAKE was the top-ranked eligible satellite with a free seat.
Three independent ways of resolving that contradiction (one seat, a 10% sleeve, or no floor)
all land at +16.2% to +18.3% and Sharpe 0.62 to 0.68 against the shipped +12.47% and 0.515 at
the same drawdown — a plateau, not a peak. It remains a defect fix and not an edge: every one
of those books still loses to buying and holding Bitcoin (+32.98%) and sits far below the
deflated hurdle of 2.32. And the most useful thing this loop found about itself is that the
live journal cannot see its own biggest finding: a position the strategy wanted and the gate
refused to open leaves a log line and no row, so until item 4 of §11 exists, this cause is
invisible in production.
