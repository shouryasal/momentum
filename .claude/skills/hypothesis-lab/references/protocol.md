# The protocol, and the measured failure behind each step

Every rule here exists because something in this repo's own record went wrong without it.
None of them is methodological taste.

## 1. Pre-register, and seal it

The seal is a SHA-256 over the statement, the falsifier, the horizon, the parameter count,
the regimes, the costs and the surface. It is recomputed at close time, and a mismatch is
fatal.

The reason it is a hash rather than a paragraph of instructions: a falsifier that can be
edited once the result is known is not a falsifier, and no wording prevents the edit. This
is the mechanical form of the discipline the rest of this page describes.

**A falsifier is a number.** "If it does not work" is not one. "If the top funding bucket's
forward drawdown lands within 3pp of the base rate, this is dead" is — it is checkable from
the same data, by someone else, without asking the author what they meant.

## 2. Costs are always on, and always the measured ones

Costs come from `config/backtest.yaml`, TCA-calibrated monthly. Not a round number chosen
because it felt conservative, and never zero "just to see the signal cleanly".

The measured context that makes this cheap to obey: at Earn's size **slippage is not the
cost**. Walking the live 1000-level book gives 0.001 bps of slippage on BTCUSDT and 0.019
bps on ETHUSDT at every size this system will trade, against a **10 bps** fee. So the cost
model is almost entirely the fee and the number of orders — which means a result that only
survives with costs off is not marginal, it is turnover-financed nonsense. The 4h-versus-1d
comparison is the worked example: identical rule, Sharpe 1.14 → 1.13, fee drag
1.2%/yr → **2.6%/yr**, drawdown −34.0% → −39.9%.

## 3. Both baselines, every time

- `btc_buy_and_hold` — **+194%** over 2021-2026.
- `strategy_baseline` — the shipped strategy over the same window, **+75.4%**.

Quoting one baseline is how a candidate that loses to buy-and-hold gets sold on beating the
shipped strategy. It is also how the opposite happens: vol targeting is **Sharpe-neutral by
construction** (buy-and-hold BTC 2017-2026: CAGR 38.9% / vol 66.9% / Sharpe 0.83 / MaxDD
−83.2%; vol-target-40%: 28.8% / 42.5% / Sharpe **0.81** / MaxDD −62.4%) and selling it as an
alpha improvement is the most common honest-looking lie in this space. Report both and the
lie has nowhere to live.

## 4. Split by regime, and print n beside every number

Bull/bear, or high-vol/low-vol, or the trend × vol quadrant. A result that exists in one
regime and not the other is a regime bet and must be sold as one.

Split-half stability is the specific thing to look for, because it is what separates the two
most similar-looking outcomes in this repo's record:

- **Survived:** funding → forward drawdown. P(7d dd < −8%) rises 17.0% → 41.4% across
  buckets, and the ratio to baseline *strengthened* across halves (1.30 → 1.46) even as
  absolute levels fell.
- **Died:** open-interest deleveraging → forward return. H1 gave +1.83% at a 67% hit rate;
  H2 gave −0.18% at 46.5%. Linear t went −3.66 → −0.04. **A full-sample backtest shows
  +1.16% and ships a corpse.**

Both look identical in a single full-sample number. Only the split tells them apart.

## 5. Walk forward; the out-of-sample number is the result

Expanding in-sample windows, fixed out-of-sample spans. Purged and embargoed folds — call
`edge-audit`, which owns the splitter, rather than writing another one. The in-sample number
is a diagnostic to report beside the OOS number, never the headline.

Why this is not optional here: overlapping labels destroy the sample. Triple-barrier labels
on BTC 4h give **19,879 rows with average uniqueness 0.152 → about 3,020 effective
independent observations**, roughly 600 per fold in a purged 5-fold CV. A leak between folds
is therefore not a small bias; it is most of the apparent signal.

## 6. Four parameters, and report the plateau rather than the peak

The cap is four. The reason is arithmetic, not aesthetics: on a 9.1-year sample the sd of an
annual Sharpe estimate is 1/√T ≈ 0.33, so the expected *best* in-sample Sharpe from N
zero-skill trials is **N=10 → 0.86 · N=50 → 1.05 · N=200 → 1.19 · N=1000 → 1.33**. Every
extra knob multiplies N.

The worked example, and the shape to imitate in every report: MA200 alone scores Sharpe
**0.76 — below buy-and-hold**. Vol targeting alone scores 0.81. The product scores
**0.82-1.38 across every MA from 50 to 250**, with months worse than −10% falling from 23 to
0-6. *The stability across the whole range is the finding.* The MA50 peak at 1.38 is barely
outside search noise and is the trap. The defensible choice is the middle of the plateau
(MA125, Sharpe 1.17, MaxDD −28.6%).

## 7. Report both directions of every rule

A filter that dodges the crashes by also dodging the rallies is worthless, and a single
headline number hides that perfectly. So state, on the same sample: what the rule avoided,
and what it gave up.

The measured instance to keep in mind: the drawdown-ladder scaler *looks* like risk
management and is a forced momentum trade on your own equity curve. Base MA100×volTarget:
CAGR 34.7%, Sharpe 1.14, MaxDD −34.0%. A flat ×0.5 scalar: CAGR 17.4%, Sharpe **1.14
(identical)**, MaxDD −18.3%, **zero** months below −10%. The ladder (1.0/0.5/0.25×): Sharpe
**1.00 — worse** — and it still drew down deeper (−23.8%) than the flat scalar. One number
would have made the ladder look fine.

## 8. Lean on edge-audit; do not re-implement it

`edge-audit` owns, and this skill only *checks that they were supplied*:

| Number | Call |
|---|---|
| effective sample size | `audit_stats.py labels --pair … --tf …` |
| purged, embargoed folds | `audit_stats.py cv --pair … --tf …` |
| the trial counter (only grows) | `audit_stats.py trials --add "<what was searched>"` |
| the deflated hurdle | `audit_stats.py hurdle --baseline <benchmark sharpe>` |
| how long live testing would take | `audit_stats.py power --sharpe-a … --sharpe-b …` |

Two of its refusals bind here directly: **a claim quoting a row count instead of an
effective N is not reportable**, and **beating the baseline is not the bar** — the bar is
`baseline + expected_max_sharpe(N, T)`.

## 9. The honest negative is the output, most of the time

A refuted hypothesis is finished work. It removes a candidate permanently, and when a human
agrees it earns a line in `research-scout`'s rejection ledger, which lowers the cost of every
future search. The measured record this system runs on is mostly negatives — meta-labelling
at 0.496 accuracy against a 0.503 base rate, the HMM at Sharpe 0.78 *with deliberate
look-ahead*, taker imbalance at t = +0.11, the CME gap at 68.4% against a 61.9% control —
and those negatives are the reason nothing here has shipped a corpse.

Write the negative up with the same care as a positive. A study that can only be reported
when it wins is not a study.

## 10. And a good live quarter is still not validation

Distinguishing Sharpe 1.14 from 0.83 at 80% power needs about **245 years**; even 2.00
against 0.83 needs about 15. A 90-day live period proves the plumbing works and nothing
whatever about edge. Attach `years_to_detect` to any claim that a live window validated
something, and say the number rather than the sentiment.
