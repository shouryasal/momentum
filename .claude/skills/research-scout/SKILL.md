---
name: research-scout
description: Finds candidate edges for Earn from the data the repo can actually obtain — local candles, the wired derivative and on-chain features, the whitelisted news archive, the verified free endpoints — and refuses any idea already on the standing rejection ledger or any source that is paywalled, keyed, laggy or unbacktestable. Triggers on look for an edge, what should we research, is there a signal in, find a new feature, research scout, is this idea already dead.
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Research scout

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

This is the **search** half of Earn doing its own research. It decides *where to look* and,
far more often, *where not to bother*. It measures nothing and concludes nothing: the
moment an idea survives this skill it is handed to `hypothesis-lab`, which states the
falsifier before any number is computed, and to `edge-audit`, which decides whether the
number means anything.

The reason this skill exists is arithmetic. Every idea costs a trial, and the trial counter
only grows (`edge-audit`: `expected_max_sharpe(N, T)`). At N=10 zero-skill trials the best
in-sample Sharpe on this repo's 9.1-year sample is already **0.86**; at N=200 it is
**1.19**. Re-testing something the repo has already killed does not cost nothing — it
raises the bar for every honest idea that comes after it. **A search that does not know what
has already been searched is not a search, it is a random walk with a report attached.**

## When this runs

- The `discover` stage (`prompts/stages/discover.v1.md`), every time, as its first step.
- Whenever a `review` or `daily_review` session is about to propose looking into something
  new, before any backtest is run.
- On demand: "is there anything in X", "should we look at Y", "has this been tried".

It does **not** run to justify a change that has already been decided. That is
`strategy-lab`.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| The standing rejection ledger | `scripts/scout.py ledger` | `python3 ${CLAUDE_SKILL_DIR}/scripts/scout.py ledger` |
| Search surfaces actually available | `scripts/scout.py surfaces` | `python3 ${CLAUDE_SKILL_DIR}/scripts/scout.py surfaces` |
| What is not obtainable free | `runs/features/__init__.py: NOT_AVAILABLE_FREE` | `python3 ${CLAUDE_SKILL_DIR}/scripts/scout.py unavailable` |
| Which feature keys have audited provenance | `runs/features/__init__.py: REGISTRY` | the same `surfaces` call |
| Prior measured results, with their split-half tables | `docs/design/crypto-research.md`, `docs/design/wide-universe.md` | read them, do not re-derive them |

Numbers come from the scripts and from those two documents. Never estimate a return, a
t-stat, a sample size or an endpoint's history depth from memory — the endpoint history
depths in particular are counter-intuitive and were verified by failure (see
`references/where-to-look.md`).

## Procedure

1. **Say the idea in one sentence**, in the form *"<observable> predicts <target> at
   <horizon>"*. An idea that cannot be written that way is not yet an idea; it is a mood.
   `predicts what` matters more than `predicts`: this repo's whole measured record says the
   public leverage data forecasts the **second moment** (volatility, drawdown) and not the
   first (return).

2. **Ask the ledger before anything else.**

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/scout.py check --idea "<the one sentence>"
   ```

   `verdict=rejected` ends it. Quote the ledger's `reason` verbatim in your report and stop
   — do not "re-test it properly", do not "try it on a different timeframe". Each entry is
   there because it was **measured and killed**, and the ledger records the measurement. If
   you believe an entry is wrong, the path is a `changes/*.json` against this skill's
   reference pages with new evidence, reviewed by a human — not a quiet re-run.

   `verdict=observe_only` means the idea is real but **cannot be backtested** from free
   data. It may accrue its own forward record; it may not enter a decision.

3. **Find the surface.** `scout.py surfaces` lists what this repo can actually read, with
   the history depth and lag of each. Prefer, in order:
   - the **local candle store** (`data/binance/<PAIR>-<tf>.feather`, 2017-08 onward, ~1.06M
     candles) — deepest, free, already point-in-time;
   - the **wired feature modules** (`runs/features/*`: Deribit DVOL, funding, open interest,
     basis, book depth, the bulk archive, the macro calendar) — already loaded, already
     trap-handled, already registered;
   - the **whitelisted news archive** in `knowledge/earn.db` — corroborated by the
     two-source rule, and the only text source this system may cite;
   - a **new free endpoint**, last, and only after step 4.

4. **Gate the source before you get attached to it.** Four questions, and all four must
   pass:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/scout.py gate --name "<source>" \
     --free yes --keyless yes --history-days <N> --lag-min <M> --backtestable yes
   ```

   - **Free and keyless.** A paywalled or account-scoped series cannot be re-pulled by an
     automated run and cannot be replayed. `needs_key` is a rejection, not a caveat.
   - **Deep enough.** The default floor is 730 days, and it is a floor rather than a target.
     Every `fapi/…/futures/data/*` endpoint retains about **30 days** — a backtest built on
     one silently has a one-month sample and will look magnificent.
   - **Low lag.** A value older than the decision cadence is not an input, it is a memory.
     Real-time SOPR/NUPL is withheld for 7 days; a 7-day-old number cannot inform a 1d
     decision and is barred from the decide feature set.
   - **Backtestable, i.e. point-in-time.** A forward-only stream (the liquidation
     websocket) and a vendor-restated series (exchange balance history: 10 days) both fail
     this. `observe_only` is the honest label; a "backtest result" on 10 days is noise with
     a number attached.

5. **Write the scouting note.** One short block per surviving idea: the one-sentence
   hypothesis, the surface, the gate result, the nearest ledger entry and why this idea is
   *not* it, and the trial cost you are about to spend. Then hand it to `hypothesis-lab`.
   Nothing here measures anything.

6. **Record the dead ends you personally hit**, in the report — not in the ledger. The
   ledger is tier 2 and a human adds to it, because a search process that can edit its own
   list of things-not-to-search is not constrained by it.

## Hard stops

- **A ledger hit is final for this run.** `verdict=rejected` ⇒ no measurement, no backtest,
  no "quick check". Quote the reason and move on.
- **Never assert a number from an unavailable source.** If `unavailable` names it (US spot
  ETF flows, historical liquidations, token unlocks, coin-days-destroyed, real-time
  SOPR/NUPL, exchange balance history, bridge volumes, CME data), the correct output is
  "not obtainable", never an estimate, a proxy presented as the thing, or a number recalled
  from training.
- **Never emit a direction.** This skill finds places to look. It does not say buy, sell,
  weight, or "bullish". The `decide` stage does that, at tier 4, from code-computed numbers.
- **Never widen the universe.** Universe membership is tier 2. A finding about an asset
  outside the tradeable set is an *input*, not a new position.
- **A proxy is not the thing it proxies.** Free and backtestable is necessary, not
  sufficient: a session-hours spread standing in for ETF flow has no measured relationship
  to the flow it stands for, and a validated *source* is not a validated *signal*.
- **Never measures anything.** No backtest, no t-stat, no Sharpe. It writes nothing outside
  `knowledge/` and `reports/`.

## Output

A scouting note appended to the run's report (`reports/…`), and optionally
`knowledge/scouting/<run_id>.json` — written by the session with its `Write(knowledge/**)`
grant, not by the script, which only ever prints — containing per idea:
`idea`, `ledger_verdict`, `ledger_hits`, `surface`, `gate` (the four booleans and the
reason for any failure), and `handoff: hypothesis-lab | dropped`. Every dropped idea is
listed with its reason — **the dropped list is the useful half of this output**, because it
is what stops the next run spending the same trial.

## References

- `references/where-to-look.md` — the search surfaces in detail: what each one holds, how
  far back, how stale it gets, and the four loader traps that silently corrupt a backtest.
- `references/rejected-ledger.md` — every standing rejection in prose, with the measurement
  that killed it. The machine-readable copy the script reads is tier 2 and is the authority;
  this page explains it.
- `references/source-test.md` — the four gates, why each one exists, and the verified
  failures behind the numbers.
