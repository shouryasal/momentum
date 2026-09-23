# Edge-audit method, with the numbers it was calibrated on

Every figure here was computed in this repo on **2026-09-23** from the local feather
candles, by the scripts in `scripts/`. Nothing is quoted from a paper.

If a script and this page disagree, **the script is wrong** — raise it in the weekly review.

---

## 1. Effective sample size

Triple-barrier labels: upper barrier `+pt·σ`, lower `−sl·σ` in log-return space with σ an
EWM standard deviation of past returns, and a vertical barrier `max_bars` ahead. The label
is which barrier the forward path touches first.

The labels are not the point. **Their overlap is.** A label spanning bars *t…t₁* shares
those bars with every other live label; per-label *average uniqueness* is the mean of
`1/concurrency` over its span, and `EFFECTIVE_N = Σ uniqueness`.

Measured, BTC 4h, `pt=2σ, sl=1σ, 30-bar limit`, full history 2017-08-17 → 2026-09-23:

| quantity | value |
|---|---|
| bars | 19,930 |
| labelled rows | 19,879 |
| mean average uniqueness | **0.1519** |
| **EFFECTIVE_N** | **3,020.5** |
| rows per independent sample | 6.58 |
| mean label span | 7.89 bars |
| max concurrency | 31 |
| label mix (down / vertical / up) | 59.5% / 1.1% / 39.4% |
| EFFECTIVE_N per fold, 5-fold | **604** |

BTC 1d with a 10-bar limit: 3,273 rows, uniqueness 0.1766, EFFECTIVE_N 578.

**The rule.** A claim quoting 19,879 rows is quoting a sample 6.6× larger than the one it
has. `check_claim()` refuses it.

---

## 2. Purged, embargoed cross-validation

Contiguous K-fold over label start times. A training label whose span overlaps the test
window **at all** is dropped — it was fitted on bars the test is about to score. Then the
labels starting in the `embargo` bars immediately after the test window are dropped too,
because serial correlation leaks in the other direction.

Measured on the same panel, 5 folds with a 1% embargo (199 bars):

| fold | train | test | purged | embargoed | leaks |
|---|---|---|---|---|---|
| 0 | 15,701 | 3,976 | 3 | 199 | 0 |
| 1 | 15,699 | 3,976 | 5 | 199 | 0 |
| 2 | 15,681 | 3,976 | 23 | 199 | 0 |
| 3 | 15,682 | 3,976 | 22 | 199 | 0 |
| 4 | 15,899 | 3,975 | 5 | 0 | 0 |

The leak count is asserted, not asserted-in-a-comment: `cv_report` re-checks every fold
and reports `LEAKING` rather than a score if any training label overlaps its test window.

---

## 3. The deflated hurdle

A system that auto-proposes changes **is** a search process, and the expected best
in-sample Sharpe from N zero-skill trials over T years is

```
expected_max_SR(N, T) = ((1−γ)·√(2 ln N) + γ·√(2 ln(N·e²))) / √T      γ = 0.5772
```

Computed here, T = 9.1 years:

| N trials | 10 | 50 | 200 | 1000 |
|---|---|---|---|---|
| expected best Sharpe | 0.862 | 1.050 | **1.187** | 1.329 |

So a candidate must clear `baseline + expected_max_SR(N, T)`, not the baseline.
`review.change_gates.walk_forward_min_out_sample_delta: 0.0` is a coin-flip hurdle against
a search whose expected best is 1.19 — that is a config finding, not a code one, and it
belongs in a change proposal.

`n_trials` lives in `knowledge/state/trial_counter.json` and **only grows**. A counter that
resets when somebody forgets is a hurdle that only ever falls.

---

## 4. Power: what live testing can and cannot prove

`Var(ŜR) ≈ (1 + SR²/2)/T` (Lo 2002). For two independent tracks — which a sleeve and its
benchmark are — the years needed to separate them at power `1−β`:

```
T = (z_α + z_β)² · (2 + (SR_a² + SR_b²)/2) / (SR_a − SR_b)²
```

| comparison | two-sided α=0.05 | one-sided |
|---|---|---|
| 1.14 vs 0.83 | **244.6 years** | 192.6 |
| 2.00 vs 0.83 | 24.9 years | — |

The design document quoted 219 years for the first row; that figure sits between the
one- and two-sided conventions. This skill reports both and labels them, rather than
picking the flattering one.

Consequence to state out loud in every post-mortem: `modes.live.min_test_days: 90` proves
the **plumbing** works and nothing whatsoever about edge.

---

## 5. The decay panel and the retirement rule

Each quarter end, every feature is re-tested on a **rolling 12-month window** with
Newey-West errors at the forecast overlap, and reported with two numbers: the t-stat and
`ratio_to_full` (the window slope over the full-sample slope). **`|t| ≤ 1.5` for two
consecutive quarters ⇒ `recommend_retire`.**

Why the rolling window rather than the full sample: a signal can post a healthy
full-sample t while being dead for years, and a full-sample backtest ships it. Measured on
BTC 1d, forward 3-day return, full history:

| feature | full-sample t | last 4 quarters | first retire signal | verdict |
|---|---|---|---|---|
| `range_pct` | **+1.83** | +0.45 +0.28 −0.14 +0.34 | 2018Q1 | RETIRE |
| `mom_20` | +1.54 | −0.15 +0.20 +0.03 +0.43 | 2018Q2 | RETIRE |
| `mom_60` | +1.69 | −0.19 −0.22 −0.91 −0.17 | 2018Q2 | RETIRE |
| `dist_ma200` | +1.41 | −0.91 +0.42 −0.42 −0.69 | 2019Q2 | RETIRE |
| `volume_z` | +1.40 | −0.89 −1.22 −1.40 −0.85 | 2019Q1 | RETIRE |
| `rv_z` | +0.82 | +0.15 −0.41 −0.01 +0.21 | 2019Q2 | RETIRE |

Read that table as a result, not as a demo. **None of the ordinary price-derived features
survives its own re-test**, and the strongest of them, `range_pct`, posts a full-sample
+1.83 while its recent quarters sit between −0.14 and +0.45. The same shape on 4h data:
`range_pct` full-sample t **+2.10**, first half **+1.87**, second half **+0.30**. A
full-sample backtest would have shipped it.

The `--self-test` case constructs the same shape with a known break — a feature that
predicts for three years and then stops — and asserts the panel fires `recommend_retire`
at 2022Q1 despite a full-sample t of **+16.5**. That is the proof that the rule catches a
corpse, which is what this skill was added for.

---

## 6. What this skill refuses, and why each refusal was earned

| Refusal | The failure it prevents |
|---|---|
| a claim quoting `n` with no `effective_n` | overlapping labels overstate the sample 6.6× on this repo's own panel |
| a CV score with no purge | a training label overlapping the test window has already seen it |
| a CV score with no embargo | serial correlation leaks across the fold boundary the purge does not cover |
| "beats the baseline" | the expected best from 200 zero-skill trials is 1.19 — beating 0.83 is not evidence |
| resetting `n_trials` | a forgotten counter is a hurdle that only falls |
| retiring a feature directly | retirement is a `changes/*.json` proposal; this skill never edits a strategy |

---

## 7. Methods this repo will not use at this sample size

Recorded so a future run does not re-propose them:

* **Deep learning on OHLCV.** ~3,000 effective samples and a handful of genuine regimes in
  nine years. Parameter count over information.
* **Meta-labelling.** Run honestly — expanding purged walk-forward, 5-day embargo — it
  scored OOS accuracy 0.496 against a 0.503 base rate, and as a filter cut Sharpe from
  1.16 to 0.80. The binding constraint is effective sample size, so no new model fixes it.
* **HMM / changepoint regime detection.** Fitted with deliberate full-sample look-ahead —
  the most flattering possible test — it still scored below a plain trend × vol-target
  rule.
* **Widening the universe to "today's liquid listings".** That selects on survival: the
  delisted coins are not in the klines API at all.
