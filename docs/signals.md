# Signals — scanner → validator → planner → gate

Between "something happened in the market" and "an order was placed" there are four stages,
each cheaper than the one after it, and each able to stop the whole thing. The design rule
is that **an expensive model is only ever asked about something a free function already
found interesting**, and that no model anywhere decides whether an order is allowed.

Everything here is configured under `config/earn.yaml: signals` and
`config/models.yaml: tasks`. The console's **Signals** page (`/signals`) shows the funnel,
the per-detector and per-model hit rates, and every signal's full evidence.

```
ingest (*/15) ─▶ knowledge.db + knowledge/state/freshness.json
                     │
scanner (*/5)  ──────┴─▶ features → detectors → dedupe → [fast path] → screener → score
                                                              │
validator (detached, one at a time) ◀─────────────────────────┘
      evidence pack → strong model → verdict + confidence + thesis + invalidation
                                                              │
planner ───────────────────────────────────── TriggerEngine.guards() ─▶ research_run --signal-id
                                                              │
                                                       proposal (schema-validated)
                                                              │
risk gate (17 deterministic checks) ─▶ Freqtrade ─▶ order
```

`signals.integration` picks the implementation: `pipeline` (this document) or `legacy`
(the original `TriggerEngine` behaviour). The shipped default is `pipeline`.

---

## 1. Scanner — every 5 minutes

`python -m runs.signals scan`, under `flock -n ops/locks/cron-scanner.lock` and a 240-second
`timeout`. It also runs straight after ingest when `signals.scanner.run_after_ingest` is
true.

### 1a. Features — pure Python, no model

`runs/signals/features.py` builds one numeric record per pair from the knowledge DB: 1h/4h/1d
returns, RSI, ATR%, realised volatility, distance from the 200-day MA, the 20-day breakout
level, a volume z-score, funding, open-interest delta, spread, drawdown, and news counts.
Numbers in, numbers out — nothing is estimated by a model.

### 1b. Detectors — deterministic, ten of them

`runs/signals/detectors.py`. The first five are the original `TriggerEngine` conditions,
moved here unchanged; the other five are new. Each produces
`Candidate(detector, pair, direction, strength)`.

| Detector | Fires on | Default |
|---|---|---|
| `news_event` | a corroborated `hack`/`depeg`/`delist`/`lawsuit`/`outage` story | on |
| `regime_flip` | the market-state regime changes | on |
| `move` | 2.5 % / 1h, 5 % / 4h, 8 % / 24h | on |
| `near_stop` | an open position approaching its stop | on |
| `funding` | \|8h funding\| ≥ 0.0010 | on |
| `breakout` | 20-day high/low on 1d, confirmed on close | on |
| `rsi_extreme` | 4h RSI-14 below 25 or above 75 | on |
| `volume_spike` | 1h volume z-score ≥ 3 over 72 bars | on |
| `dip_from_high` | 12 % off the 30-day high | on |
| `ma_cross` | 50/200 on 1d | **off** |

### 1c. Dedupe

`dedupe_key = detector:pair:direction:bucket(dedupe_minutes)` with
`dedupe_minutes: 240`. A repeat inside the bucket is the same signal, not a new one. Rows
land in `signals` with `status='candidate'`.

### 1d. Fast path — no model at all

`near_stop`, and `news_event` for `hack`/`depeg`/`delist`, go straight to `status='valid'`
**by rule**, skipping both the screener and the validator. When your position is about to
be stopped out or the asset just got delisted, waiting 90 seconds for a model is the wrong
trade-off.

### 1e. Screener — the cheap model

`llm.run_task('scan')`, chain `[local_small, haiku]`, at most
`max_candidates_per_cycle: 5` per cycle, 90-second deadline, `tools: none`, one turn. Output
is validated against `schemas/signal_screen.json`, and then the **host** verifies every
cited `feature_key` actually exists and every `news_hash` is real — a model cannot cite
evidence it invented. A novel claim needs at least one corroborated news hash.

A score in the gray zone (`[0.45, 0.65]`) is re-run on the next entry in the chain with
reason `escalate:gray_zone`.

### 1f. Score

```
score = 0.6 × detector_score + 0.4 × screen_score
```

`≥ signals.scanner.screen.min_score` (0.60) ⇒ `status='screened'`, otherwise
`'screened_out'`. If the screener is unavailable the detector score stands alone and the row
records that it did.

Each screened signal then spawns a **detached** validation:

```
flock -n ops/locks/validate.lock timeout <deadline> \
  bash ops/envwrap.sh signals -- python -m runs.signals validate --signal-id <id>
```

---

## 2. Validator — one at a time, 600 seconds

`runs/signals/validator.py`. First it builds a **deterministic evidence pack** — pure JSON,
saved to `journal/snapshots/signals/<id>/pack.json`, so the exact inputs can be replayed
later.

Then `llm.run_task('validate')`: chain `[sonnet, opus]`, escalation `opus`, `min_tier: 3`,
read-only tools, and the `market-state` and `asset-dossier` skills for Claude (a local model
would get the pack only — but it can never serve this task anyway: tier 3 is a **code**
floor). Output is checked against `schemas/signal_validation.json`.

The result is a `signal_validations` row: verdict (`valid | invalid | uncertain`),
confidence, thesis, reasons, **counter-evidence**, the **invalidation condition**, a
suggested direction and horizon, the pack path, cost and latency.

The signal is acted on only if **all three** hold:

* `verdict == 'valid'`,
* `confidence >= signals.validator.min_confidence` (0.65),
* the suggested direction is not `hold`.

Validator throttles: `max_per_day: 6`, `cooldown_min_per_asset: 120`,
`expire_after_min: 90` (a signal older than that is stale and is dropped).

---

## 3. Planner — the handoff

Before firing anything, the signal asks `TriggerEngine.guards()` — **the** guard
implementation, not a copy. It blocks on:

| Guard | Meaning |
|---|---|
| `kill` | `ops/killdir/KILL` exists |
| `cooldown` | a `decide` stage already ran inside `signals.planner.cooldown_hours` (4) |
| `daily_cap` | `signals.planner.max_per_day` (3) already fired today |
| `stale_data` | data age exceeds `ops.staleness_min` (30 min), from `knowledge/state/freshness.json` |

If nothing blocks:

```
bash ops/envwrap.sh research -- python -m runs.research_run <HHMM> \
     --triggered-by signal:<id>,<detector reason> --signal-id <id>
```

`--triggered-by` is a comma-separated list and carries both halves: `signal:<id>` first,
so the run row names the signal it came from, then the detector's human-readable reason
(`move_4h:BTC:-6.0`). A scheduled or legacy run has no signal and passes the reasons
alone. `TriggerEngine.triggered_by()` is the one place that shape is built.

The decision stage then receives a **validated-signal block** — the candidate, its features,
the validator's thesis and the invalidation condition — and runs with forced escalation. The
proposal it produces is validated host-side against `schemas/proposal.py`, written
atomically, and journaled with its `signal_id`.

**Observe-only rollout:** `signals.planner.enabled: false` runs everything up to and
including the verdict and then stops. You get the full funnel and the hit rates with no
research run and no order. This is the right setting for the first weeks.

### Signal statuses

`candidate → screened_out | screened → validating → valid | invalid | uncertain → blocked |
planned → acted`, plus `expired` and `error`.

---

## 4. The gate still stands

A proposal is not an order. `SleeveB`'s `bot_loop_start` reads the targets,
`custom_stake_amount` / `adjust_trade_position` size them, and `strategies/riskgate.py`
applies 17 deterministic checks in this order before anything reaches the exchange:

`nav_valid`, `kill`, `monthly_lock`, `daily_lock`, `blackout`, `staleness`, `reconcile`,
`trades_per_day`, `orders_per_day`, `turnover_day`, `fee_budget`, `min_notional`,
`order_notional`, `entries_per_trade`, `weight_cap`, `gross_cap`, `usdt_floor`.

A discretionary exit (a trim, a take-profit rung) is additionally gated by
`orders_per_day`, `turnover_day` and `fee_budget`. **Risk exits are never blocked** — a
flatten can never be starved by the limit it just tripped.

`nav_valid` is first for a reason: the gate's NAV is **ledger NAV** — the bot's own capital
(`starting_balance + realised closed profit + realised/unrealised P&L of bot-owned open
trades`, with USDT reserved in open entry orders counted). If the Freqtrade objects it needs
are missing, the portfolio state is invalid and entries are refused. Fail closed.

---

## 5. Outcomes and replay

`signals.outcomes`: after `resolve_after_hours` (24), measured on the `measure_tf` (1h)
candles, `python -m runs.signals resolve` writes `outcome_ret` and `outcome_hit` back onto
the validation row. Those resolved outcomes are what turn the funnel into real precision and
recall, and `evals/signal_replay.py` scores a **candidate** scanner or validator prompt
against them — which is how a prompt change to `scan` or `validate` earns its evidence in the
change gate (see [autonomy.md](autonomy.md)).

---

## Providers and automatic switching

The model layer is `runs/llm/`. Every task has an **ordered chain**, not a preference. The
first entry that (a) clears the task's `min_tier`, (b) has the capabilities the task needs,
and (c) is not behind an open circuit breaker or an exhausted budget, serves the call.

### Who can serve what

| Alias | Provider | Model | Tier |
|---|---|---|---|
| `fable` | claude | `claude-fable-5-1` | 5 |
| `opus` | claude | `claude-opus-5` | 4 |
| `sonnet` | claude | `claude-sonnet-5` | 3 |
| `haiku` | claude | `claude-haiku-4-5-20251001` | 2 |
| `local_small` | ollama | `llama3.1:8b` | 1 |

| Task | Chain | Floor | Tools |
|---|---|---|---|
| `scan` | `local_small → haiku` | 1 | none |
| `classify` | `local_small → haiku` | 1 | none |
| `flags` | `haiku → local_small` | 1 | read-only |
| `brief` | `sonnet → haiku → local_small` | 1 | skills |
| `validate` | `sonnet → opus` (escalate `opus`) | **3, in code** | read-only |
| `decide` | `opus` (escalate `fable`) | **4, in code, never local** | read-only |
| `review` | `fable → opus` | 4 | skills |
| `daily_review` | `fable → opus` | 4 | skills |

`MIN_TIER_FLOOR` and `ALWAYS_LOCAL_FORBIDDEN` live in `runs/llm/types.py`. Lowering
`min_tier` in `models.yaml`, or through the tier-1 overlay, changes nothing: `chain_for()`
enforces the floor for every consumer, including the test fakes.

### Two providers, three credentials

`claude:subscription`, `claude:api_key` and `ollama`. Which Claude credential a job gets is
`EARN_CLAUDE_AUTH_MODE` (mirrored from `models.yaml: auth.claude_mode`), applied by
`ops/envwrap.sh`:

| Mode | The job's environment |
|---|---|
| `subscription` | `CLAUDE_CODE_OAUTH_TOKEN` only — a present API key is dropped, because it would preempt subscription auth in a headless run |
| `api_key` | `ANTHROPIC_API_KEY` only |
| `auto` | the token (if any) **plus** the key renamed to `EARN_FALLBACK_ANTHROPIC_API_KEY` |

The rename in `auto` is unconditional. A name the Claude CLI cannot pick up implicitly is
the whole point: metered spend is only ever a deliberate, journaled `claude:api_key`
attempt.

Ollama is detected automatically (`providers.ollama.base_url: auto`), probing
`127.0.0.1:11434`, the WSL default gateway, the `resolv.conf` nameserver and
`host.docker.internal` in that order. An explicit URL must be loopback or private. See the
README for the WSL→Windows networking fix, or the **AI & Models** page, which prints it for
your machine.

### When and how it switches

`models.yaml: switching.on` maps a failure class to an action:

| Failure | Action |
|---|---|
| `error`, `timeout`, `rate_limited`, `quota_exhausted`, `budget_exhausted`, `auth_error`, `empty_output` | `next` |
| `provider_down` | `skip` |
| `schema_invalid` | `retry_then_next` |

Plus `max_attempts_per_call: 4`, a circuit breaker (3 failures in 15 minutes opens it for
15), `prefer_local_when_rate_limited: [scan, classify]`, and escalation on `hard_case`,
`trigger`, `gray_zone` or `low_confidence`.

`budget_exhausted` and `quota_exhausted` are **terminal** — never retried.

**Nothing switches silently.** Every attempt is an `llm_calls` row and every move down a
chain is a `provider_switches` row with its reason, both visible on the **AI & Models** and
**Decisions** pages, which show requested model vs served model side by side.

When a whole chain fails, `on_all_failed` decides what happens, per task:
`scan → skip_screen`, `classify → rule`, `flags`/`brief` → `keep_last`,
`validate → drop_signal`, `decide → hold_last`, `review`/`daily_review` → `abstain`.
A missing model never becomes a guess.

---

## Costs and caps

Under a Claude Max subscription there is no metered spend, and `budget.mode: telemetry`
says so: the USD figures in the journal are **estimates**, degradation keys on the
subscription's own rate-limit signals, and the per-run `max_usd_per_run` values exist as
runaway-loop safety, not as a bill.

| Scope | Key | Default |
|---|---|---|
| Whole system | `budget.monthly_total_usd` | \$150 (telemetry) |
| Throttle point | `budget.throttle_at_pct` | 80 % — the 16:00 brief drops first; **decide runs are never skipped** |
| Metered spend, any time the API key serves | `auth.api_key_monthly_cap_usd` | \$30 — a **hard** cap, and `budget.mode` is ignored for it |
| Shadow model | `shadow.monthly_budget_usd` | \$20 |

Per task, `max_usd_per_run` / `monthly_budget_usd` / `deadline_s`:

| Task | per run | per month | deadline |
|---|---|---|---|
| `scan` | \$0.05 | \$3 | 90 s |
| `classify` | \$0.20 | \$5 | 120 s |
| `flags` | \$0.40 | \$4 | 240 s |
| `brief` | \$1.00 | \$12 | 360 s |
| `validate` | \$1.50 | \$30 | 600 s |
| `decide` | \$4.00 | \$60 | 900 s |
| `daily_review` | \$8.00 | \$40 | 3000 s |
| `review` | \$25.00 | \$70 | 6000 s |

The shape of the spend is deliberate: the two tasks that run most often (`scan` every five
minutes, `classify` on every news item) are the two that a free local model serves first.
The expensive models are reserved for the decisions that are actually rare — six validations
a day at most, three research runs, one nightly review, one weekly review.

Usage is on the **AI & Models** page grouped by task, model, provider, auth source or day,
and every row traces back to an `llm_calls` entry.
