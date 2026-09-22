

# Earn — Autonomous Crypto Trading System: Planning Document

2026-09-22 · 

## 1. Purpose, scope and non-negotiables

Earn is a spot-only, crypto-only trading system that runs unattended on Shourya's own hardware, with Claude as the analyst and Freqtrade as the executor. It is built to be tested on paper for three months, then funded with seed money it can afford to lose.

Scope (v1)
- 

Universe: BTC, ETH and at most three other top-10 non-stablecoin assets on Binance (UAE entity), USDT pairs only.
- 

Cadence: 4-hour and daily candles. No intraday scalping, no market making, no arbitrage.
- 

Phases: paper with three parallel sleeves → live in propose mode (Telegram approve/reject) → live in execute mode.
- 

Out of scope until v2: futures, margin, leverage, Earn products, alts outside the top 10, equities (an IBKR adapter comes later).

Non-negotiables
- 

Claude never sits in the order path. A deterministic risk gate validates every order before Freqtrade sends it.
- 

Buy-and-hold BTC is the benchmark. The system earns its place through smaller drawdowns and discipline, not fast gains.
- 

Every change to strategy parameters, prompts or skills is logged and gated. Risk limits, key handling and execution code are human-only.
- 

Costs are measured, not assumed: fills are reconciled against decision-time quotes and the results feed the backtester monthly.

## 2. System architecture and data flow

The system is two Freqtrade bots, one deterministic risk gate, and two scheduled Claude runs that never touch the exchange. Everything Claude produces is a file that code validates before anything reaches Binance.
```
 KB[(knowledge/SQLite + markdown)]
  NEWS[Whitelisted news RSS] --> KB
  ALT[Free alt data] --> KB
  KB --> RES[Research runClaude, 2x daily]
  LES[lessons.md + skills] --> RES
  RES --> PROP[proposals/*.json]
  PROP --> FTB[Freqtrade BClaude sleeve]
  KB --> FTA[Freqtrade Arules sleeve]
  FTA --> GATE{Risk gatestrategy callbacks}
  FTB --> GATE
  GATE --> EX[Binance spottrade-only key]
  EX --> JRN[(journal/ SQLite)]
  JRN --> TCA[TCA + Excel view]
  JRN --> REV[Review runClaude, weekly]
  REV --> LES]]>
```


Reading: data flows left to right into the knowledge store; the research run turns it into proposals; Freqtrade turns proposals or rules into orders only after the gate passes them; fills flow back into the journal, which feeds the transaction-cost analysis and the weekly review that rewrites the lessons and skills the next research run reads.

Component

What it does

Tech

Who may change it

knowledge/

Candles, indicators computed by code, news archive with source and corroboration flags, exchange rules, per-asset records

Python, SQLite, markdown

Claude, freely

Research run

Reads knowledge, journal and lessons; writes a proposal file that fits a JSON schema

Claude Agent SDK, project skills

Prompt and skills: Claude, gated

proposals/

One JSON per run: target weights, module choice, confidence, rationale, abstain flag

JSON, pydantic-validated

Claude output only

Freqtrade A

Rules sleeve: DCA + trend filter + volatility targeting

Freqtrade strategy class

Parameters: Claude, gated

Freqtrade B

Claude sleeve: same limits, reads the latest valid proposal

Freqtrade strategy class

Parameters: Claude, gated

Risk gate

confirm_trade_entry, custom_stake_amount, protections; rejects any order outside limits

Freqtrade callbacks, unit tests

Human only

Execution

Binance spot via Freqtrade's exchange layer; paper adapter for evaluation

Freqtrade, ccxt

Human only

journal/

Proposals, gate decisions, orders, fills, daily NAV per sleeve vs benchmark

SQLite (record), openpyxl (Excel view)

Code writes; nobody edits

TCA

Decision-time quote vs fill VWAP, fee, slippage in bps; monthly cost calibration

Python

Human only

Review run

Grades the week's decisions, updates lessons.md, proposes parameter, prompt and skill changes

Claude Agent SDK

Output gated (section 7)

Ops

Scheduler, health checks, Telegram alerts, backups, kill switch

cron or Task Scheduler, Python

Human only

## 3. How Claude interacts: surfaces, account and billing

Build with Claude Code (the VS Code extension is the same agent with a GUI), run the unattended loops with the Claude Agent SDK in Python, and keep this chat for design reviews. No surface talks to the exchange; the runs read the repo and write files.

Surface

Role in Earn

Unattended?

Claude Code CLI in the terminal

Building, debugging, backtests, weekly interactive review of the diffs the review run proposed

No (interactive)

Claude Code VS Code extension

Same agent as the CLI with a sidebar and inline diffs; use whichever you prefer for building

No

claude -p headless mode

Runs non-interactively from cron: prints the result and exits, with --allowedTools, --output-format json and per-run turn and dollar caps ([https://docs.claude.com/en/docs/claude-code/headless] docs). Quickest way to get the research run working in week 4

Yes

Claude Agent SDK (Python)

Same tools, agent loop and context management as Claude Code, as a library ([https://code.claude.com/docs/en/agent-sdk/overview] docs); loads the repo's .claude/ skills with setting_sources=["project"]. The production path for research_run.py and review_run.py

Yes

Claude Desktop app with connectors

Ad-hoc questions against connectors; not part of the loop

No

Claude.ai chat (this project)

Planning, architecture reviews, reading the journal exports

No

Why the Agent SDK for the runs. It gives structured programmatic control, subagents with isolated context, and hooks that mirror Claude Code, with prompt caching and compaction on by default. The runs need exactly that: read-only tools, a fixed skill set, a hard budget, and a JSON result the gate can validate. claude -p does the same job with less control and is fine until the SDK version is stable.

Account and billing. Unattended runs authenticate with an API key from the Claude Console; claude.ai login and subscription rate limits are not allowed for third-party agents, and from June 15, 2026 Agent SDK and claude -p usage on subscription plans draws from a separate monthly Agent SDK credit ([https://tutorialsdojo.com/claude-agent-sdk/] summary; confirm on the current pricing page). Every run logs its total_cost_usd into the journal, and each run carries a dollar cap, so the monthly bill is known by the end of week 4.

Statelessness. Each run starts with an empty context. Continuity lives in the repo — CLAUDE.md, lessons.md, the journal and the skills — never in chat memory. That is deliberate: what the system knows is inspectable and versioned in git.

## 3a. Model routing

One model per task, picked by runs/router.py from config/models.yaml, with an escalation ladder for hard cases and a shadow mode for promoting a new model. Every proposal, brief and review records the model and prompt version that produced it, so quality per model is measured, not assumed.

Task

Default model

Why

Escalation / fallback

News ingest: dedupe, classify, extract fields, corroboration check

Haiku 4.5

High volume, structured extraction, cheapest

Fallback: rule-based parser. Never escalates

Daily brief and market-state narrative

Sonnet 5

Synthesis over a few thousand tokens, twice a day

Fallback: Haiku 4.5 with a shorter brief

Decision run (proposal)

Opus 5

Judgement under constraints, twice a day

Escalates to Fable 5.1 on a hard-case flag; on error retry once, then keep the last valid proposal and alert. Never downgraded silently

Weekly review, post-mortem, tier-1 change proposals

Fable 5.1

Highest-stakes reasoning, once a week, low volume

Fallback: Opus 5, with its change proposals held for human approval

Replay evaluations

Same model as the task under test

A replay on a different model compares nothing

—

Code and skill edits in Claude Code sessions

Opus 5 for edits, Fable 5.1 to review tier-1 diffs

Cost-effective editing, strongest review

—

Telegram digests, health summaries

Haiku 4.5

Formatting only

Plain template, no model

Hard-case flags (any one escalates the decision run to Fable 5.1): regime change in the last 48 hours; trend and volatility modules disagree; drawdown within 1% of a stop; a reg-watch flag is active; two consecutive abstains; TCA cost above threshold.

Rules. Model strings are pinned in models.yaml (claude-fable-5-1, claude-opus-5, claude-sonnet-5, claude-haiku-4-5-20251001); the Agent SDK takes the model per run and each subagent can be pinned separately. Each task has a monthly budget; at 80% the brief frequency drops before any decision run is skipped. A new or different model runs in shadow on the decision task for 30 days — same inputs, proposals stored but not traded — and is promoted only if process grades and agreement rate are at least as good; promotion is a tier-1 change. The weekly report shows decisions, validity rate, process grade and cost per model.

## 4. Running locally

The laptop is enough for the paper phase; live money moves to a mini-PC at home or a small VPS, because the Binance IP whitelist needs a static address and home broadband does not give one.

Hardware and OS. Paper: the laptop, plugged in, sleep disabled, Docker Desktop installed. Windows runs everything through WSL2; macOS and Linux run natively. Live: a fanless mini-PC on the home network with a static route, or a VPS in a nearby region — the VPS gets the static IP for free and survives power cuts.

Processes

Process

Schedule (Gulf time)

Runtime

Writes

freqtrade-a

always on

Docker container, dry-run in paper

journal (sleeve A)

freqtrade-b

always on

Docker container, dry-run in paper

journal (sleeve B)

ingest.py

every 15 min

Python

knowledge: candles, book snapshots, news

research_run.py

08:30 and 16:00

Agent SDK, dollar-capped

proposals/YYYY-MM-DD-HHMM.json

tca_job.py

hourly

Python

journal: cost per fill, rolling cost per sleeve

review_run.py

Sunday 20:00

Agent SDK, dollar-capped

lessons.md, changes/ proposals, weekly report

excel_view.py

after each run

openpyxl

reports/earn.xlsx

healthcheck.py

every 5 min

Python

Telegram alert on stale data, dead container, gate breach

backup.sh

daily 03:00

shell

SQLite copies to a second disk or cloud bucket

Scheduler. cron on Linux and macOS; Task Scheduler on Windows, or cron inside WSL2 with a systemd-enabled distro. Every job is idempotent and safe to rerun; a lock file stops overlapping runs.

Always on. Containers run with restart: unless-stopped; host jobs run as systemd timers (or Task Scheduler entries) so they come back after a reboot with automatic login. healthcheck.py restarts any container that has not written a heartbeat in 10 minutes and reruns a scheduled run once if its output file is missing 30 minutes after schedule, then alerts. API calls retry with exponential backoff inside a per-run deadline; a run that overruns is killed and counted as missed. Sleeve B never waits on Claude: with no valid proposal it holds its last targets and after 48 hours follows sleeve A. On the laptop this means sleep and hibernation off on mains power, lid-close set to do nothing, and updates scheduled outside run times; anything more reliable than that is the mini-PC or VPS.

Repository layout
```

```


Secrets. Binance key and secret, the Telegram token and the Anthropic API key live in .env with permissions 600 (or the OS keychain), are passed to containers as environment variables, and are listed in .gitignore; a pre-commit secret scanner blocks accidental commits. The research and review runs get no exchange credentials at all: their process environment carries only the Anthropic key.

Network. Outbound only. Freqtrade's REST/UI server binds to localhost; the Telegram bot is the only inbound channel and accepts commands from one chat ID. No port forwarding on the home router.

## 5. Skills catalogue

Ten skills, all in .claude/skills/<name>/SKILL.md inside the repo so they are versioned, loaded by the Agent SDK runs and by Claude Code sessions, and uploadable to claude.ai if needed. Each is a folder with a SKILL.md (YAML frontmatter plus instructions) and optional scripts and reference files; the description in the frontmatter is the trigger, and where the folder lives decides which sessions load it ([https://code.claude.com/docs/en/skills] docs).

Skill

Purpose

Inputs

Outputs

Invocation

Edits allowed

crypto-brief

Daily brief from the whitelist with the two-source rule; fork of last30days-skill

RSS/API pulls, knowledge/news

knowledge/briefs/YYYY-MM-DD.md with source links and corroboration flags

Auto, research run

Claude, free

market-state

Compute and describe regime: trend, realized vol, drawdown, funding, breadth, using code not estimates

candles, funding, OI

knowledge/state/latest.json and one paragraph

Auto, research run

Claude, free (scripts gated)

decide

The decision procedure: read state, brief, open positions, lessons; choose module and target weights inside limits; write the proposal

knowledge, journal, lessons.md, config/earn.yaml

proposals/*.json validated against schemas/proposal.json

Auto, research run

Body: Claude, gated

exchange-ops

Binance reference: LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL, rate-limit weights, order types, error codes, recvWindow

—

Correct order parameters and error handling in code changes

Background knowledge only (user-invocable: false)

Claude, free

tca

Execution-quality procedure: decision-time quote capture, fill reconciliation, cost in bps, thresholds, monthly calibration into backtests

journal fills, book snapshots

reports/tca-weekly.md, updated cost assumptions

Auto, review run; /tca on demand

Human only

strategy-lab

Evaluation protocol: hypothesis → backtest with costs → walk-forward → parameter sensitivity → paper → live, with overfitting checks

strategy code, data

changes/*.json with backtest deltas and replay results

Auto, review run; /strategy-lab

Body: Claude, gated

risk-gate

The limit set the callbacks enforce, protections config, breach handling, weekly risk report format

config/earn.yaml, journal

reports/risk-weekly.md

/risk on demand; review run

Human only

post-mortem

Grade each decision on process and outcome separately; maintain lessons.md with dated, falsifiable entries

journal, proposals, outcomes

lessons.md appends, graded table

Auto, review run

Claude, free (append-only)

reg-watch

VARA, CMA, SEC and Binance notices: delistings, stablecoin depegs, licence changes; sets blackout flags

regulator and exchange feeds

knowledge/flags.json the gate reads

Auto, research run

Claude, free

ops-runbook

Restart, recover from exchange outage, rotate keys, pause a sleeve, restore a backup

ops scripts

Executed steps and an incident note

Manual only (disable-model-invocation: true)

Human only

Conventions. Descriptions are written in the third person with the phrases that should trigger them. Bodies stay under 500 lines; heavy reference goes in references/, executable steps in scripts/. Skills with side effects carry disable-model-invocation: true; background knowledge carries user-invocable: false; each skill pre-approves only the tools it needs with allowed-tools. Every skill has a tests/ case the replay harness runs before an edited version is accepted.

## 6. Decision loop

Claude decides what exposure to hold, code decides whether that is allowed and how to execute it. Indicators are computed by Python and handed to Claude as numbers; Claude never estimates a price, an RSI or a volatility from memory.
```
>R: 08:30 / 16:00
  R->>K: read state, brief, positions, lessons
  R->>P: write proposal (schema-validated)
  B->>P: load newest valid proposal
  B->>G: confirm_trade_entry / custom_stake_amount
  G-->>B: allow or reject (reason logged)
  B->>X: order (dry-run in paper)
  X-->>K: order_filled -> journal + TCA snapshot]]>
```


What the research run reads. knowledge/state/latest.json (trend, realized vol, drawdown, funding, breadth), today's brief, open positions and NAV per sleeve, the last 30 graded decisions, lessons.md, flags.json from reg-watch, and the current limits copied from config/earn.yaml. Total context is kept under a fixed token budget by code before the run starts.

What it may output. Only what the schema allows:
```

```


No prices, no order types, no leverage, no assets outside the universe. A proposal that fails validation is discarded and logged; Freqtrade B then keeps its last valid targets, and after 48 hours without a valid proposal it drifts to sleeve A's targets.

What the gate enforces (Freqtrade callbacks: confirm_trade_entry, confirm_trade_exit, custom_stake_amount, custom_entry_price, order_filled, plus protections MaxDrawdown, StoplossGuard, CooldownPeriod — [https://www.freqtrade.io/en/stable/strategy-callbacks] callbacks, [https://www.freqtrade.io/en/latest/includes/protections/] protections):
- 

Target weight per asset ≤ cap; gross exposure ≤ cap; USDT floor respected.
- 

Daily and monthly loss stops flatten the sleeve and lock new entries for the configured cooldown.
- 

Trades per day ≤ cap; minimum notional met; no entries in blackout windows or while a data-staleness flag is set.
- 

Limit-maker entries by default (custom_entry_price at the best bid/ask); market orders only for stop exits.
- 

order_filled writes the fill with the decision-time quote so TCA can compute cost the same hour.

Cadence rationale. Two runs a day match 4-hour and daily candles and keep API cost predictable. Anything faster adds cost and turnover without adding edge at this horizon.

## 7. Self-learning and self-improvement

The system learns by rewriting its own context, parameters and skills through a gate — not by training the model. Claude's weights never change; what changes is what each run reads and the numbers the strategies use, and every change is a git commit that can be reverted.

Three tiers of self-change

Tier

What Claude may change

When

Condition to apply

0 — free

Knowledge store, news archive, briefs, lessons.md (append-only, dated), its own notes in knowledge/

Every run

Passes schema and lint; nothing else

1 — gated

Strategy parameters inside human-set bounds (lookbacks, vol target, exposure scale, module choice rules), prompt text, skill bodies for decide, strategy-lab, market-state

Weekly review run

A changes/*.json proposal with rationale, unit tests green, backtest with measured costs over ≥ 2 years, walk-forward delta ≥ threshold, decision replay on the last 30 days, ≤ 2 parameter changes per month. Paper: auto-merged. Live, first 3 months: Telegram approval

2 — human only

Risk limits and bounds, universe, autonomy flag, capital per sleeve, execution code, key handling, tca, risk-gate, ops-runbook

Never by Claude

Edited by Shourya in a normal Claude Code session, with tests

Mechanics. The review run works on a branch: it edits files, runs pytest and the backtest and replay scripts, and writes changes/<date>-<slug>.json with the deltas. apply_changes.py merges proposals that meet the thresholds and reloads Freqtrade's config; rejected ones stay on the branch with the reason. A Claude Code PreToolUse hook blocks any write to tier-2 paths during automated runs, so the boundary is enforced by tooling, not by instructions.

Learning from the journal. Each decision is graded twice: process (was the thesis consistent with the state and lessons at the time, did it respect the invalidation rule) and outcome (P&L versus the rules sleeve over the stated horizon). Only process grades feed lessons.md directly; outcome grades feed statistics that need at least 30 decisions before any lesson may cite them. This stops the system learning "buy dips" from one lucky week.

What went wrong, and whether to learn from it. Every losing week, every gate rejection and every missed run gets a diagnosis before any lesson is written. The post-mortem skill assigns each event one root cause, and the cause decides what may change and how soon.

Root cause

Example

Fix path

Learn?

Data

Stale candle, missed news item, wrong funding value

Code or feed fix, tier 0

Yes, immediately

Execution and cost

Slippage above assumption, partial fills, fee tier changed

TCA calibration, order-type policy; human

Yes, after two weeks of evidence

Ops

Missed run, container down, proposal not loaded

Runbook fix; human

Yes, immediately

Reasoning

Proposal ignored its own invalidation rule, skipped the checklist, overrode a flag

Prompt or decide skill change, tier 1

Yes, once the pattern repeats three times

Strategy and parameters

Trend filter whipsawed in a range, vol target too high for the regime

strategy-lab change, tier 1

Only with ≥ 30 decisions and out-of-sample support

Market noise

Sound process, adverse outcome

None

No — recorded, never a lesson

The deciding test is a counterfactual replay: the review run re-runs the week with the proposed fix and reports whether the outcome or the process grade changes. A fix that changes neither is not applied. A root cause that recurs three weeks running escalates to Shourya with the evidence, because a loop that keeps diagnosing the same thing is itself the fault.

Cost calibration. Monthly, tca_job.py replaces the fee and slippage assumptions in config/backtest.yaml with measured medians. If measured cost exceeds the backtest assumption by more than 50% for two weeks, the review run's parameter changes are frozen until the gap is explained.

Drift and overfitting guards. Parameters have bounds and a maximum step per change. Walk-forward uses expanding windows; a change that wins in-sample but loses out-of-sample is rejected automatically. The review run must list what would make each lesson false, and lessons older than 180 days without re-confirmation are archived out of the active file.

What this will and will not do. Expect better discipline, tighter cost control and fewer repeated mistakes within months. Do not expect the loop to discover new alpha: the gains come from context and parameters, and they diminish. The honest measure of learning is the process-grade trend and the TCA trend, not the P&L line.

## 8. Prompt engineering

Yes, there is prompt engineering, but it is versioned code with an evaluation harness, not something you do by hand between runs. Every proposal records the prompt version that produced it, so decision quality can be compared across versions.

Where prompts live. prompts/research.vN.md and prompts/review.vN.md, assembled at runtime by runs/build_prompt.py from fixed sections plus the day's inputs. CLAUDE.md holds the standing rules every run reads first; the skills hold the procedures; the prompt holds the task, the inputs and the output contract.

Structure of the research prompt
- 

Role and horizon (spot-only analyst, 7-day horizon, benchmark is holding BTC).
- 

Hard constraints copied from config/earn.yaml at run time, so the prompt can never disagree with the gate.
- 

Inputs by path, with the token budget already applied by code.
- 

A decision checklist taken from strategy-lab: regime, trend, cost since last rebalance, open invalidations, blackout flags, reasons not to trade.
- 

Few-shot examples: the two best- and two worst-graded past decisions from the journal, refreshed monthly by the review run.
- 

The output contract: the proposal schema, abstain: true as the default when inputs are stale or conflicting.

Evaluation harness. evals/replay.py re-runs a candidate prompt over the last 30 decision days with the inputs as they were, and scores: constraint violations (must be zero), schema validity rate, agreement rate with the rules sleeve when the state is unambiguous, calibration of stated confidence against outcomes, and turnover implied by the proposals. A prompt change is a tier-1 change: it ships only if the replay is at least as good on every metric and better on one.

Techniques worth using. Numbers in, numbers out — the model reads computed indicators and writes weights; static context first for prompt caching; an explicit abstain path; a required invalidation statement per decision; low variance by design (the same inputs should give the same proposal, checked by running the replay twice).

Techniques to avoid. Open-ended "analyse the market" prompts, asking the model to fetch and read raw news pages inside the decision run, letting it see the P&L of the last few trades (it anchors on them), or editing prompt wording mid-week because one decision looked wrong.

Shourya's role. Read the weekly diff of prompt and skill changes with their replay scores; edit tier-2 files; add examples to the journal when a graded decision deserves it. No live prompt tweaking.

## 9. Risk, security and controls

Limits are numbers in config/earn.yaml that only Shourya edits; the defaults below are starting points for paper and are deliberately tight for the first live months.

Control

Default

Enforced by

Max weight per asset

40% of sleeve NAV (BTC), 30% others

custom_stake_amount, confirm_trade_entry

Max gross crypto exposure

80% (USDT floor 20%)

gate

Daily loss stop

−3% of sleeve NAV: flatten, lock entries 24 h

gate + MaxDrawdown protection

Monthly loss stop

−10%: sleeve paused until reviewed

gate; resume is human-only

Max trades per day per sleeve

4

gate

Consecutive stop-outs

3 in 48 h locks entries 24 h

StoplossGuard

Cooldown after exit

2 candles

CooldownPeriod

Data staleness

No candle or book snapshot in 30 min → no entries, alert

bot_loop_start check + healthcheck

Blackout windows

Reg-watch flags; 1 h around scheduled US CPI and FOMC

gate reads flags.json

Kill switch

ops/KILL file present → cancel open orders, no new entries, alert

every loop, every run

Proposal validity

Schema fail → discarded; 48 h without a valid one → follow sleeve A

Freqtrade B loader

Key hygiene. One Binance API key per bot instance, permissions Enable Reading and Enable Spot & Margin Trading only, withdrawals disabled, IP-restricted once on a static address, rotated every 90 days through ops-runbook. Read-only keys for the paper phase. Keys never appear in prompts, logs, journal rows or chat.

Capital isolation. Only the allocated amount sits on the exchange; the rest stays in the bank. Live starts with an amount whose total loss changes nothing.

Monitoring. healthcheck.py alerts on Telegram for: container down, stale data, gate breach, proposal failures two runs in a row, TCA cost above threshold, NAV drop beyond daily stop, backup failure. A daily 21:00 summary (NAV per sleeve vs BTC, trades, cost bps, open flags) confirms the system is alive; silence is itself an alert.

Incident playbook (in ops-runbook): exchange outage → bot pauses on its own, resume when order status reconciles; suspected key compromise → delete key in Binance first, then KILL, then rotate; runaway trading → KILL, /forceexit all on Telegram, inspect journal; model outage → sleeve B follows sleeve A after 48 h automatically.

Regulatory footing. Use only a licensed exchange (Binance's UAE entities hold VARA and ADGM licences); no leverage or derivatives in v1; personal spot trading needs no licence, and UAE levies no personal income or capital-gains tax on it — confirm the last point with a tax adviser before scaling.

## 10. Evaluation gates and go-live criteria

Sleeve B earns live money only by beating both the rules sleeve and holding BTC over at least 90 paper days on net return and drawdown, with clean operations throughout. A shorter or noisier test proves nothing, and even this one is a screen, not proof of edge.

Gate

Metric

Threshold to pass

G1 Plumbing

Testnet round trips, fill reconciliation, kill switch, restore from backup

All pass in a rehearsal, twice

G2 Backtest

Sleeve A over ≥ 2 years with measured costs; walk-forward

Max drawdown better than holding BTC; net return ≥ 60% of BTC's; costs < 1% of NAV per month

G3 Paper, 90 days

Net return, max drawdown, turnover, cost bps per sleeve vs benchmark

B ≥ A and B ≥ hold-BTC on net return; B drawdown ≤ A's; fees + slippage < 1% NAV/month

G4 Process

Proposal validity, gate breaches, abstain use, process grades

Validity ≥ 95%; zero gate breaches; process grade trending up over the last 6 weeks

G5 Ops

Healthcheck incidents, missed runs, data staleness events

≤ 2 incidents/month, none unexplained

G6 Live propose, 30 days

Same as G3 and G4 with real fills; TCA gap

Measured cost within 50% of paper assumption; you approve ≥ 80% of proposals

G7 Scale

After 90 further live days meeting G3–G5

Allocation may double once per 90 days, never faster

Failure handling. A failed G3 does not end the project: sleeve A can go live alone if it passes G2–G5, and sleeve B keeps running on paper with the review loop until it earns its place. A failed G4 or G5 pauses everything until the cause is fixed and the gate is rerun.

Reporting. reports/earn.xlsx carries the gate table with live values; the Sunday review run marks each gate pass or fail with the evidence row, so the go-live decision is a reading of the sheet, not a judgement call under pressure.

## 11. Build plan week by week

Four build weeks, then a 90-day paper run with the review loop live from week 5. Each week has a "done when" that is checkable, not a feeling.

Week

Deliverables

Done when

1

Binance UAE account with BNB fee deduction on; spot testnet keys; Claude Console API key; repo scaffold, CLAUDE.md, config/earn.yaml; Docker + Freqtrade installed; 3–5 years of 1h/4h/1d candles for the universe; knowledge/earn.db and journal/ schemas

Freqtrade dry-run starts and stops cleanly on both machines; candles load with no gaps

2

Sleeve A strategy (trend + vol target + DCA) with the risk gate callbacks; tests/ for every limit; backtests with 10 bps fee + 5 bps slippage; sleeve C benchmark script; excel_view.py v1

G2 numbers in the sheet; every gate test green; a deliberately bad order rejected in dry-run

3

ingest.py (book snapshots, whitelisted news, funding); TCA module with decision-time capture and fill reconciliation; Telegram alerts; healthcheck; backups; testnet round trips

G1 rehearsal passed twice; TCA report shows cost bps per testnet fill

4

Skills crypto-brief, market-state, decide, exchange-ops, reg-watch; research_run.py on the Agent SDK (or claude -p first); proposal schema and validator; models.yaml and router.py with the escalation flags; Freqtrade B reading proposals

Two runs a day produce valid proposals for 5 consecutive days; sleeve B trades on them in dry-run

5

Skills post-mortem, strategy-lab, tca, risk-gate, ops-runbook; review_run.py; evals/replay.py; apply_changes.py; PreToolUse hook protecting tier-2 paths

First Sunday review produces a graded table, a lessons.md entry and at least one gated change with replay scores

6–17

90-day paper run; monthly TCA calibration; weekly gate table; move to mini-PC or VPS around week 12

G3–G5 evaluated on day 90

18–21

Live in propose mode with seed capital; trade-enabled key on a static IP

G6 evaluated

22+

Execute mode; scale only per G7

—

Effort. Weeks 1–5 are evenings and weekends with Claude Code doing most of the typing; the human hours go into reviewing the gate tests, the backtest assumptions and the prompt. From week 6 the workload is one Sunday hour reading the review.

What can slip. Binance onboarding for the UAE entity, testnet instability, and Windows scheduling quirks are the usual delays; the fallback for each is Bybit, the local paper adapter, and cron inside WSL2.

## 12. Open decisions for Shourya

Eight choices shape the scaffold; the plan assumes the first option in each unless told otherwise.
- 

Runtime for the runs: Agent SDK on an API key (assumed) or claude -p on the subscription's Agent SDK credit.
- 

Machine: Windows laptop with WSL2 for paper (assumed), or a mini-PC/VPS from day one.
- 

Exchange: Binance (assumed) or Bybit.
- 

Universe: BTC and ETH only (assumed) or BTC, ETH plus up to three large caps.
- 

Tier-1 changes during paper: auto-merged when thresholds pass (assumed) or Telegram approval from the start.
- 

Seed capital and the loss you can shrug off, which sets the daily and monthly stops in absolute terms.
- 

Alert channel: Telegram (assumed) or email.
- 

Decision-run model: Opus 5 with escalation to Fable 5.1 on hard-case flags (assumed), or Fable 5.1 for every decision run at higher cost.

## Sources

Documentation pages this plan relies on; fees and limits are approximate and must be verified in the live account.
- 

[https://docs.claude.com/en/docs/claude-code/headless] Claude Code: headless mode
- 

[https://code.claude.com/docs/en/skills] Claude Code: skills
- 

[https://code.claude.com/docs/en/agent-sdk/overview] Claude Agent SDK overview
- 

[https://tutorialsdojo.com/claude-agent-sdk/] Agent SDK billing note, June 2026
- 

[https://www.freqtrade.io/en/stable/strategy-callbacks] Freqtrade strategy callbacks
- 

[https://www.freqtrade.io/en/latest/includes/protections/] Freqtrade protections
- 

Binance spot fees: 0.10% base, 0.075% with BNB deduction (approximate; check the account's fee page)
## APPENDIX — code blocks from the doc (lost in the text conversion above)
```
flowchart LR
  BIN[Binance public API] --> KB[(knowledge/SQLite + markdown)]
  NEWS[Whitelisted news RSS] --> KB
  ALT[Free alt data] --> KB
  KB --> RES[Research runClaude, 2x daily]
  LES[lessons.md + skills] --> RES
  RES --> PROP[proposals/*.json]
  PROP --> FTB[Freqtrade BClaude sleeve]
  KB --> FTA[Freqtrade Arules sleeve]
  FTA --> GATE{Risk gatestrategy callbacks}
  FTB --> GATE
  GATE --> EX[Binance spottrade-only key]
  EX --> JRN[(journal/ SQLite)]
  JRN --> TCA[TCA + Excel view]
  JRN --> REV[Review runClaude, weekly]
  REV --> LES
```

```
earn/
  CLAUDE.md                # what every Claude run must know first
  config/                  # earn.yaml (limits, universe, sleeves, autonomy flag), freqtrade-a.json, freqtrade-b.json
  .claude/skills/          # the skills in section 5, versioned
  prompts/                 # research.vN.md, review.vN.md
  schemas/                 # proposal.json, change.json
  knowledge/               # earn.db (SQLite), rules/, assets/, news/
  strategies/              # SleeveA.py, SleeveB.py, riskgate.py (shared callbacks)
  proposals/  changes/  journal/  reports/  lessons.md
  runs/                    # research_run.py, review_run.py, tca_job.py, ingest.py, excel_view.py
  tests/                   # risk gate, schema, TCA, replay evals
  ops/                     # docker-compose.yml, crontab, healthcheck.py, backup.sh
```

```
sequenceDiagram
  participant C as cron
  participant R as research_run.py
  participant K as knowledge/ + journal
  participant P as proposals/
  participant B as Freqtrade B
  participant G as Risk gate
  participant X as Binance
  C->>R: 08:30 / 16:00
  R->>K: read state, brief, positions, lessons
  R->>P: write proposal (schema-validated)
  B->>P: load newest valid proposal
  B->>G: confirm_trade_entry / custom_stake_amount
  G-->>B: allow or reject (reason logged)
  B->>X: order (dry-run in paper)
  X-->>K: order_filled -> journal + TCA snapshot
```

```
{
  "run_id": "2026-09-22T08:30+04:00",
  "prompt_version": "research.v3",
  "module": "trend",              // trend | dca | cash | hold
  "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
  "exposure_scale": 0.8,          // 0..1 applied on top of vol targeting
  "confidence": 0.6,
  "abstain": false,
  "horizon_days": 7,
  "rationale": ["BTC above 200d, vol regime medium", "no blackout flags"],
  "invalidation": "BTC daily close below 200d MA"
}
```

