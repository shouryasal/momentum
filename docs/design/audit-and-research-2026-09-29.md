# Audit and research — 2026-09-29

**Status:** decision-grade. Written by the orchestrator after the synthesis agent of
`.wf-audit-research.js` died on a session limit; every number below is lifted from the
workflow's own journal (`wf_5b6a7e89-744`) or re-derived here, not from that agent's summary.

**Why it exists (owner, verbatim):** *"continue improving the solution we need most profit.
audit complete code and do research to see what we can do to improve. read through research
papers and then test."*

**Method.** Four auditors by area (order path, signals/LLM, ops/console, evals/data), each
required to produce a repro before reporting; every finding then put to **three independent
verifiers** with different lenses (is the claim true of the current code / does the repro
demonstrate it / would it lose money or break a safety invariant), majority rule, default
refuted. Then three literature sweeps — trend and allocation, carry and leverage, execution
and costs — each pre-registering its hypotheses and falsifiers in the hypothesis ledger
**before** computing anything, testing on the survivorship-free panel with costs always on,
purged and embargoed walk-forward, per-regime splits, and the deflated hurdle applied.

128 agents, 40 findings, **39 confirmed**, 12 hypotheses tested.

---

## 0. The answer

**Nothing in the literature or the data raises this book's return.** Twelve pre-registered
hypotheses were tested and eleven were refuted outright; the twelfth (vol targeting) survives
only as a *priced drawdown option* — it buys 8.9 points of drawdown for 1.4 points of CAGR.
Two things survive as **arithmetic rather than alpha**: paying Binance fees in BNB (0.075%
against 0.100%, worth **+0.215% of NAV a year** on the ensemble and **+1.71%/yr** on the
plumbing profile) and yield on idle USDT (**about +0.5pp of CAGR per 1% APR** on a book that
is half in cash). Neither is an edge; both are money.

**The profit is in the defects, not in a new strategy.** The audit found 39 confirmed bugs,
and the ones that cost money are not subtle: a validation pipeline where **every answer ever
produced was discarded host-side** and the failures then consumed the budget that would have
paid for a successful one; a holdings watcher that has been reading the wrong database for
six days; our own spending caps being counted as Anthropic outages and opening the circuit
breaker on the strong models; two scanners racing every fifteen minutes and paying twice.
The system has been buying model answers and throwing them away.

**And one correction to this project's own headline.** `dip-strategy.md` §0.3 reports Sharpe
as CAGR ÷ vol. On the estimator the rest of the repo uses, BTC hold is **0.83, not 0.58**, so
the ensemble's edge is **1.33×, not 1.86×** — and §6.2's "1.08 against a 2.07 hurdle"
compared a geometric number with an arithmetic one. The drawdown result (−45.6% against
−83.2%) is unaffected and remains the finding.

---

## 1. Confirmed findings, ranked by money and safety

Each was reproduced by its auditor and confirmed by at least two of three verifiers.
"Fixed" means in the working copy as of this document; "open" means not yet.

### 1.1 Money

| id | file:line | what | state |
|---|---|---|---|
| **SL-01** | `runs/signals/validator.py:307` | **Every validation ever run was discarded host-side**; 13 of 16 for citing pack paths the check did not know | fixed in the working copy |
| **SL-07** | `runs/signals/validator.py:203` | Those failures then consumed `max_per_day` **and** the per-asset cooldown — six errors a day silently stopped all validation, leaving no trace on the signal | **fixed** |
| **F1** | `strategies/earn_base.py:348` | **Sleeve B double-buy race is still open**: a resting entry order is counted in NAV but in no position, so the rebalance path re-buys the same target. Cost 27.56 USDT on 09-23, 15.01 of it fees. Hidden today only because the fast profile crosses the spread | **open** |
| **F2** | `strategies/SleeveB.py:394` | **Phantom "no proposal ever" flatten is still in the code**: any run-id change or one unreadable proposals directory sells the whole sleeve at market as `target_zero` | **open** |
| **SL-11** | `runs/ingest.py:938` | Two scanners every 15 minutes (cron `*/5` and ingest's hook) take different locks: double screening spend, racing a non-atomic dedupe | **fixed** |
| **SL-04** | `runs/signals/pipeline.py:277` | Screener down or item dropped ⇒ candidate promoted to the **strong** validator on detector score alone, against its own docstring | open |
| **SL-06** | `runs/signals/pipeline.py:95` | Detectors re-fire the identical closed-daily-candle observation every dedupe window and after every outage | open |
| **SL-09** | `runs/router.py:304` | `two_abstains` is self-perpetuating: every proposal abstains, so 6 of 6 decides since 09-24 escalated to Fable@max | open |
| **ED-1** | `runs/tca_job.py:51` | TCA books ladder exits against a stale snapshot: −80 to −157 bps of phantom "slippage" on a third of all fills | open |
| **ED-2** | `runs/tca_job.py:136` | **On 2026-10-01 the monthly calibration will overwrite the cost model with dry-run medians** — slippage 5.0 → ~2.3 bps, making every future backtest optimistic | open, dated |
| **AU3-01** | `ops/healthcheck.py:725` | Reruns keyed on `now`: a research rerun writes today's proposal file and the real slot is then skipped | open |

### 1.2 Safety

| id | file:line | what | state |
|---|---|---|---|
| **SL-05** | `runs/watch/positions.py:241` | **The holdings watcher read the wrong database for six days** — `holdings: 0` with positions open in both sleeves; every stop-proximity and invalidation check skipped | **fixed** |
| **SL-08** | `runs/llm/base.py:47` | **Our own caps counted as provider failures**: three $0.05-cap scan errors opened the credential-wide breaker for 15 min, taking `validate` and `decide` with them | **fixed** |
| **AU3-04** | `console/services/overview_service.py:493` | Home built the KILL path by hand against `REPO_ROOT`: under `$EARN_STATE_ROOT` the dashboard reads green while the gate refuses every entry | **fixed** |
| **AU3-02** | `ops/lib/tg.py:41` | **775 alerts, none ever delivered**; dedupe counts only delivered rows, so criticals repeat every tick | open |
| **AU3-03** | `.claude/hooks/tier2_paths.py:163` | Tier-2 hook bypass: write a script into tier-0 `knowledge/` or `reports/` and run it with a bare interpreter | open |
| **F4** | `strategies/mechanics.py:247` | **Live only**: the "market-only stop" invariant becomes a `STOP_LOSS_LIMIT` 1% below the stop on Binance spot — unprotected through a gap | open, blocks live |

### 1.3 Silent state and correctness

`F3` (the daily stop said `halve` while nothing halved), `F7` (the trend gate had no writer
wired), `SL-15` (the watcher ran under the `classify` task) are **fixed** — see §3.
`ED-5` (the Sharpe estimator) is **fixed**. Still open: `ED-3` (no buy-and-hold benchmark
series exists anywhere in the live journal), `ED-4` (`nav_daily` samples an intraday moment
and never rewrites), `ED-6` (symbol-reuse guard inherits age across a relist), `F5` (trailing
stop ~2×fee tighter than intended), `F6` (`beta_cap` makes any alt above 1.30 beta
unopenable at any size), `F9` (partial exits bypass `confirm_trade_exit`, so TCA is blind on
them), `SL-03`, `SL-10`, `SL-12`, `SL-13`, `SL-14`, `AU3-05` … `AU3-10`.

**The one refuted finding:** `SL-01`'s third verifier judged it already fixed in the
uncommitted working copy — which is true, and the reason it is listed as fixed above.

---

## 2. Research: twelve hypotheses, pre-registered, costed, walk-forward

### 2.1 Trend and allocation

| id | hypothesis | verdict | net-of-cost edge | drawdown effect |
|---|---|---|---|---|
| `ta-vol-target` | Scale each leg by `clip(target/σ̂, 0, cap)` using the shipped HAR+DVOL forecast | **refuted as return, survives as a drawdown option** | −8.4pp CAGR walk-forward; −1.4pp at the best fixed cell | **−36.7% vs −45.6%** (8.9pp shallower); one fewer −10% month a year; fee drag 1.36 → 2.5%/yr |
| `ta-third-asset` | BNB as a third leg, or a top-K satellite basket at 10% | refuted | BNB's +12.2pp is entirely 2021 and negative in 5 of the other 7 years; satellites −0.8pp | unchanged / +0.55pp |
| `ta-sharpe-weighting` | Weight the 15 members by trailing Sharpe | refuted — **1/N wins** | −4.9pp CAGR; member Sharpe ranks do not persist (Spearman −0.21 to +0.14) | **12pp deeper** |
| `ta-reentry` | Kaminski–Lo re-entry after a zero weight | refuted | −0.87pp | 1pp deeper |
| `ta-cash-yield` | Idle USDT into Binance Simple Earn Flexible | **survives as arithmetic** | **+1.1 to +1.5pp CAGR** at today's verified 1.5–2% APR (≈200–300 USDT/yr on 20,000) | +0.58pp shallower |

### 2.2 Carry, leverage, on-chain

All four refuted. Funding-extreme risk-off buys **1.6pp of drawdown for 7.9pp of CAGR** —
the flagged days are the book's best days. Cash-and-carry nets ~+0.1pp/yr on the book and
needs a futures mandate the spot-only risk gate has no path for. OI build-up leaves max
drawdown unchanged. Basis momentum on a single perpetual has no term structure to read and
loses 81% of capital over six years. Exchange netflow was dropped by the research-scout
ledger before measurement: not backtestable from free data.

### 2.3 Execution and costs

| id | hypothesis | verdict | worth |
|---|---|---|---|
| `bnb-fee-discount` | Pay fees in BNB (0.075% vs 0.100%, verified at VIP0) | **survives as arithmetic** | **+0.215% of NAV/yr** on the ensemble, **+1.71%/yr** on the fast profile; tail cost 0.02–0.18% of NAV on a one-month float |
| `ensemble-weight-hysteresis` | Dead band on the weight to cut turnover | refuted | +0.59pp in sample, **−0.28pp out**; 1.4pp deeper drawdown |
| `min-hold-vs-min-edge` | The k=5 minimum-holding gate as churn control | refuted | fees unchanged (10.52 → 10.51%/yr — fills are capped on the entry side); as written it converts 308 trend-loss exits into 6% stop-outs |

Maker-only entries, next-bar-open entry, hour-of-day placement and Almgren-Chriss slicing
were all dropped before measurement by the research-scout rejection ledger or by prior
measurements in `analogue-timing.md` — the literature puts them at ≤1bp against a 10bp fee.

**The honest summary:** best candidate Sharpe was 1.44 (hindsight BNB) and 1.31 (vol target)
against a deflated hurdle of **2.43** at N=7,910 trials. The `edge-audit` skill refused all
three submitted claims.

---

## 3. What was built and shipped today, and why

| change | evidence | state |
|---|---|---|
| **BTC/ETH trend ensemble** as entry gate and position scale; 15 members, equal weight, one-bar lag | reproduced to the decimal: 43.07% / −45.62% / geo 1.08 (arith 1.10) | built, **gate wired to an ingest phase** so it cannot fail closed on deploy |
| **Daily stop = `hold`** (lock entries, sell nothing) | `crisis-policy.md` §0: hold **+30.53%**, halve +13.05%, flatten −5.23% | built; replaced the `halve` the reviewer caught describing a book that held |
| Rungs 0.9/1.5 + `min_edge` ≥ 3× the round trip | the ladder replay moves the result by <3 USDT — compliance, not improvement | built |
| Satellites to 2 seats / 5% with the exclusion filter | 0 of 6,720 satellite configs beat holding BTC | built |
| Local tier: `granite4.2:3b` + lean scan prompt | the local tier has screened **nothing** since 09-23 (19–30k tokens against an 8k window) | built |
| Watcher on `holdings_watch` + the run database | `SL-15`, `SL-05` | **fixed** |
| Cold-start recovery, DNS grace, suspend detection | `outage-2026-09-25.md` | built |
| Decision-stage indicators | 7 of 7 proposals abstained on `assets: {}` | built |
| Cumulative pot + the profit & gap ledger | the console showed 20,009.68 against a true 19,939.91 | built |

---

## 4. Build list, in priority order

1. **`F2` — the phantom flatten** (`strategies/SleeveB.py`). A plumbing event that sells the
   whole sleeve at market. Tier 2. It has fired once already.
2. **`F1` — the double-buy race** (`strategies/earn_base.py`). Count resting entry orders in
   the position view, not only in NAV. Tier 2. Cost 27.56 USDT once.
3. **`ED-2` — the 2026-10-01 cost-model overwrite.** Dated: it will silently make every
   backtest optimistic on Thursday. Tier 2, one flag.
4. **`AU3-02` — alerting with no delivery path.** 775 alerts, zero delivered. Until this is
   fixed the system cannot tell anyone anything while the owner is away.
5. **BNB fee payment.** +0.215% of NAV/yr on the ensemble, certain, one setting plus a small
   inventory rule.
6. **`SL-04`, `SL-06`, `SL-09`** — the signal funnel's three leaks.
7. **Cash yield on idle USDT** — conditional on Simple Earn Flexible being offered on the
   UAE entity; needs plumbing to redeem before an entry.
8. **`F4`** before any live trading — the market-stop invariant does not hold on Binance spot.
9. **`ED-3`/`ED-4`** — there is still no benchmark series in the journal to judge anything
   against, and `nav_daily` is an intraday sample.

**Vol targeting is a decision, not a build item.** It is the only measured way to make the
book calmer (−36.7% vs −45.6%) and it costs 1.4pp of CAGR and doubles fee drag. That is a
preference about how the drawdown feels, and it is the owner's to make.

---

## 5. What would tell us in 30 days

From `dip-strategy.md` §10.1, unchanged, because 30 days still cannot measure return:
exposure tracks the ensemble weight within ±0.10 every day; fee drag ≤ 0.15% of NAV per 30
days; turnover 0.4–1.0× NAV; no 30-day drawdown worse than −12%; and the book loses less
than BTC on BTC's ten worst days. Plus, new this week and read off the profit & gap ledger:
**zero hours with no heartbeat**, **zero validations discarded host-side**, and **the local
tier serving `scan` at all**.

---

## 6. Honest limits

The audit read the working copy at a moment when five agents were editing it; three findings
concern code written the same morning. The verifiers are the same model family as the
auditors, so a shared blind spot would survive all four votes. The research is bounded by
what this repo can obtain free — no order-book history, no on-chain flows, no alternative
data — and by a spot-only, long-only mandate that `dip-strategy.md` §0.2 already showed
cannot express upward conviction beyond being fully invested. Every "survives" above is an
accounting fact, not a forecast; none of them clears a deflated Sharpe hurdle, and none of
them is claimed to.
