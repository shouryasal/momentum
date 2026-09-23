# Vol-surface definitions and the measurements behind them

Every default in `scripts/compute_volsurface.py` and `runs/features/volatility.py` is here
with the number that justifies it. All figures were computed in this repo on
**2026-09-23** from the local feather candles and a freshly paged Deribit DVOL series; none
is quoted from a paper.

If the script and this page disagree, **the script is wrong** — raise it in the weekly
review rather than describing around it.

---

## 1. The estimators

| Term | Definition |
|---|---|
| `rv_d` | Realised variance of one UTC day = Σ squared 4h log returns inside the day. The first return of a day is measured from the previous day's last close, so no overnight gap is dropped. |
| trailing vol | `sqrt(mean(rv over the last 30 days) × 365) × 100`, in vol points. |
| forward vol (the target) | `sqrt(mean(rv over the NEXT 7 days) × 365) × 100`. Strictly forward and never used as a feature. |
| HAR(d,w,m) | OLS of `log(forward vol)` on `log` mean realised variance over 1, 5 and 22 days. Expanding window, refit every 30 days, fitted only on rows whose forward target had already been observed at the prediction date. |
| DVOL map | `sigma_hat = a + b·DVOL`, same expanding-window discipline. |
| `sigma_hat` | The **equal-weight average** of the two when both exist. `source` names it. |
| `vol_target_scalar` | `clip(target_annual_points / sigma_hat, 0, 1)`. |
| `vrp` | `dvol_last − trailing_vol_30d`, in vol points. |

**Partial days are dropped.** The last UTC day is removed when it holds fewer bars than a
normal day. Without that rule a 4h feed read at 08:00 contributes two bars out of six and
prints a volatility about 40% too low, which flows straight into the scalar as false
confidence. Earlier short days (genuine exchange outages) are kept — they are complete
observations of what happened.

---

## 2. Why DVOL is in here at all

BTC daily closes 2021-03-24 → 2026-09-16, forward 7-day realised vol, Newey-West(7):

| Construction | trailing 30d | DVOL | joint |
|---|---|---|---|
| Daily close-to-close vol (n = 2,002) | R² 0.1665, t +7.72 | R² 0.2680, t +10.86 | DVOL t **+7.94**, trailing **−0.33** |
| 4h realised variance (n = 2,003) | R² 0.3312, t +10.22 | R² 0.3961, t +11.68 | DVOL t **+6.51**, trailing **+2.21** |

Read both rows. On the weaker close-to-close target DVOL appears to *subsume* trailing
realised entirely; on the better realised-variance target trailing realised survives with a
small but real t of +2.21. **DVOL dominates, it does not subsume** — and the difference is
the target construction, not the market. The second row is the honest one, and it is the
one this skill's estimators are built on.

DVOL quintiles against forward 7d realised vol (4h RV target, n = 2,003):

| quintile | mean DVOL | mean forward 7d realised |
|---|---|---|
| Q1 | 38.5 | 33.3 |
| Q2 | 47.8 | 43.7 |
| Q3 | 55.7 | 45.7 |
| Q4 | 67.5 | 54.3 |
| Q5 | 90.3 | 72.0 |

Monotone, with a 39-point spread. Split halves: H1 (2021-03 → 2023-12) R² 0.404, t +9.84;
H2 (2023-12 → 2026-09) R² 0.230, t +8.93. Decayed in level, intact in significance.

---

## 3. Why the fitted map is mandatory

Full-sample map on the 4h-RV target: **`sigma_hat = 5.37 + 0.741 × DVOL`** (R² 0.396,
n = 2,003). The variance risk premium averages **+8.13** vol points (median +6.96).

Substituting the raw index into `target_vol / sigma` overstates volatility by **+11.9% at
DVOL 35, +20.5% at 60, +25.0% at 90**. Measured over 1,946 walk-forward days, that is a
mean position scalar of 0.553 raw against 0.669 fitted on BTC — the raw index sizes
**17.3% smaller** — and 0.447 against 0.511 on ETH, **12.5% smaller**. A silent haircut
that grows exactly when the forecast is most confident.

---

## 4. Why `sigma_hat` is a blend, and not DVOL alone

Walk-forward, identical rows (2021-05-20 → 2026-09-16, n = 1,946), OOS R² against the
expanding-mean benchmark:

| model | BTC | BTC H2 | ETH | ETH H2 | BTC MAE |
|---|---|---|---|---|---|
| HAR(d,w,m) | 0.363 | 0.133 | 0.360 | 0.003 | 12.47 |
| fitted `a + b·DVOL` | 0.265 | 0.193 | 0.205 | 0.051 | 12.76 |
| HAR + log DVOL in one regression | 0.375 | 0.206 | 0.274 | 0.044 | 11.93 |
| **50/50 blend** | **0.391** | **0.238** | **0.371** | **0.090** | 11.98 |

Three things this table decides:

1. **DVOL alone is not the best forecast.** The design proposal assumed it would be; on
   identical walk-forward rows a plain HAR beats it on both assets. The in-sample
   regression in §2 compares DVOL to *one trailing window*, not to a properly specified
   HAR, and that distinction is where the assumption went wrong.
2. **The components fail in different halves.** HAR collapses in the recent half (0.133
   BTC, 0.003 ETH) exactly where DVOL holds up. That is the whole argument for carrying
   both.
3. **The weights are 50/50 on purpose.** An optimised weight is one more search trial to
   pay for at the deflated hurdle (`edge-audit`), for a few points of R².

Benchmark forecasts on the same BTC panel, for scale: EWMA(0.94) OOS R² 0.248, trailing
22-day 0.133, 7-day random walk 0.111. Every one of them is beaten by HAR, and using
yesterday's volatility as today's forecast is the weakest of the lot.

---

## 5. Health, staleness and refusal

* `oos_r2_250d` — rolling 250-day OOS R² of whatever `sigma_hat` used, scored against the
  **expanding mean of past actuals**, i.e. what a forecaster with no model would have said.
  Scoring against the window's own mean instead hands the benchmark the window's future and
  makes a working model look broken about **45% of days**.
* Below **0.05** the script withholds `sigma_hat` and states the reason.

**Where that floor actually bites, and why "no forecast" may not mean "no cap".** Measured
over the full walk-forward history (2019-04-30 → 2026-09-15, 2,696 scored days):

| | BTC | ETH |
|---|---|---|
| days under the 0.05 floor | 316 (**11.7%**) | 364 (**13.5%**) |
| when | 2020-03-02 → 05-27 · 2022-01-26 → 08-29 · 2022-09-07 → 09-22 | 2019 autumn · 2022-01 → 06 · 2025-03 → 04 · 2025-05 → 10 |
| mean forward 7d realised vol on those days | **71.2** vs 54.1 overall | 69.2 vs 69.9 overall |
| overall walk-forward MAE / bias | 14.47 / −0.42 pts | 18.71 / +0.32 pts |

Those windows are the COVID crash and the LUNA/3AC year. A rule that emitted nothing there
would have removed the volatility cap in precisely the two regimes it exists for — the
forecast is withheld, so the **scalar falls back to the more cautious of the discredited
forecast and trailing 30-day realised vol**, with `degraded: true` and
`scalar_source: trailing_30d_fallback`. Trailing realised vol is a poor *forecast*, which
is why it is not `sigma_hat`; it is a perfectly good *floor on caution*, which is the only
job it is given here.
* DVOL older than **1.5 days** is stale: the blend drops to HAR alone and `stale` is set.
* `deribit_option_oi_total` is the venue canary. DVOL's dangerous failure is going *stale
  rather than wrong* — a plausible index computed off a book nobody trades. Total option OI
  below 50% of its 90-day median means treat DVOL as suspect.

---

## 6. The source

```
https://www.deribit.com/api/v2/public/get_volatility_index_data
    ?currency=BTC&start_timestamp=<ms>&end_timestamp=<ms>&resolution=86400
```

* **Free, keyless, no account.** Verified HTTP 200 from this host on 2026-09-23.
* A **browser User-Agent is required** — Deribit resets the connection on urllib's default,
  and the failure looks like a network outage.
* At most ~1,000 points per request; the response carries a `continuation` timestamp to
  page **backwards**. `resolution` accepts 60 / 3600 / 43200 / 86400.
* **History correction.** The design document recorded ETH DVOL as starting 2023-12-27.
  That is what a single wide request returns, because the API answers with the *latest*
  1,000 points inside the span. Paging on `continuation` reaches **2021-03-24 for ETH as
  well as BTC** (2,009 daily bars each, verified), and the two series differ from the first
  day (BTC 95.04, ETH 106.73 on 2021-03-24), so it is a genuine ETH index. ETH needs no
  special fallback.
* Option surface: `get_book_summary_by_currency?currency=BTC&kind=option` — 970 instruments
  in one ~430 KB response, carrying `mark_iv`, `underlying_price` and `open_interest`.
  Deltas are recomputed locally (Black-76, zero rate) rather than copied.

**Refresh cadence.** Once per UTC day, after 00:10Z, when the previous day's bar has
closed. Only closed bars are cached, which is what makes the series replay-safe: a value
stamped `D` was knowable at `D+1 00:00Z` and never earlier. The merge is idempotent —
running it more often is harmless, running it late backfills.

**When Deribit is down**, every entry point returns the cached series with `stale=true` and
the true `as_of` of the last cached bar. Nothing invents a level.

---

## 7. What this skill deliberately does not do

* **No direction.** Volatility forecasts the second moment. Every attempt in this repo's
  research to make a volatility or leverage input directional either failed outright or
  worked and then died.
* **No skew-based sizing.** `rr25` and `butterfly` are computed and stored
  `observe_only: true`. There is no free history of the surface, so they cannot be
  backtested; they become candidates once six months of self-collected history exist.
* **No scalar above 1.0.** Not a policy — a clamp in the code, with a test.
