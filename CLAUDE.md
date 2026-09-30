# CLAUDE.md — standing rules for every Claude run and session

## What this repo is

Earn: a spot-only crypto system on Binance (UAE), BTC/USDT and ETH/USDT only. Claude
analyses and proposes target weights; code decides whether that is allowed and how to
execute it. Two sleeves run in parallel — `SleeveA` (rules) and `SleeveB` (Claude's
proposals) — benchmarked against holding BTC. Earn earns its place through smaller
drawdowns and discipline, not fast gains.

A local web console (`console/`, 127.0.0.1 only) is the human's seat: config, mode, risk,
signals, skills, prompts and the change queue all have a page. See `docs/console.md`.

## Non-negotiables

- Claude never sits in the order path. A deterministic risk gate (`strategies/riskgate.py`,
  17 checks in Freqtrade callbacks) validates every order before Freqtrade sends it.
- Numbers in, numbers out: indicators are computed by Python and handed to Claude as
  numbers. Never estimate a price, an RSI or a volatility from memory.
- `abstain: true` is the default when inputs are stale or conflicting.
- All limits come from `config/earn.yaml` — never restate a limit by hand anywhere.
- Mode is not config. Per-sleeve mode lives in the HMAC-signed `var/state/mode.json`; a
  missing, unsigned or unverifiable file means **every sleeve is TEST**.
- **A study never opens a live database.** Measurement, audit and research runs read
  `ops.lib.snapshot.latest()` — never `knowledge/earn.db`, `journal/journal.db` or an
  `ft_userdata` sqlite file, in any mode, including read-only and the backup API. On
  2026-09-30 read-copies taken while the bots traded made `healthcheck` log `database is
  locked` three times running and `ingest` write no rows for **6h42m**, and since a stale
  freshness stamp is what refuses an entry, nothing could be bought for most of a working
  day. Nothing was corrupted and nothing alerted. State which snapshot a number came from.

## Tier boundaries

The one source is `.claude/hooks/tier2_paths.py`. The PreToolUse hook
(`.claude/hooks/protect_tier2.py`) enforces it in automated runs (`EARN_AUTOMATED_RUN=1`),
and `runs/apply_changes.py` refuses to merge any commit touching a tier-2 path.

| Tier | Who | Paths |
|---|---|---|
| 0 — free | Claude, every run | `knowledge/**` (except `flags.json` and `state/**`), `reports/**`, `changes/**`, `proposals/**`, `lessons.md`, `lessons-archive.md` |
| 1 — gated, via `changes/*.json` + `runs/apply_changes.py` | Claude, in a worktree | `config/params-sleeve-{a,b}.json` (inside `bounds:`), `config/models-auto.yaml` (add an alias, move `tasks.<t>.chain[0]`, own `shadow`), `prompts/**` (incl. `prompts/stages/*.md`), every skill body and `tests/` **except** `ops-runbook`, and except `tca`/`risk-gate` `tests/` |
| 2 — human only | Shourya, a normal Claude Code session with tests | `config/**` (minus the two tier-1 carve-outs), `strategies/**`, `runs/**`, `evals/**`, `ops/**`, `console/**`, `schemas/**`, `tests/**`, `journal/**`, `data/**`, `ft_userdata/**`, `var/**`, `knowledge/flags.json`, `knowledge/state/**`, `.env*`, `.claude/settings.json`, `.claude/hooks/**`, every skill's `scripts/**`, `.claude/skills/ops-runbook/**`, `config/skills-registry.auto.yaml`, `config/prompts-auto.yaml`, `.gitignore`, `pyproject.toml`, `.pre-commit-config.yaml`, `CLAUDE.md` |

Reads denied outright in an automated run: `.env*`, `var/state/**`, `var/runtime/**`,
`**/.config/earn/**`, `**/.claude/.credentials.json`. Bash commands that reach the network
or the loopback console are refused too — an automated run never calls the console.

**Invariants no tier and no config can move** (shown read-only on the console's Invariants
page): the `127.0.0.1` bind; `EFFORT_FLOOR = "high"`; `ALWAYS_DISALLOWED`; market-only stop
exits; `decide` ≥ tier 4 and never local, `validate` ≥ tier 3
(`runs/llm/types.py: MIN_TIER_FLOOR`, `chain_for`); committed bot configs always
`dry_run: true`; unverified mode state ⇒ TEST; mode writes refuse under
`EARN_AUTOMATED_RUN=1`; the kill switch never waits for the ops lock; a `skill_new` change
touching `scripts/**` is always held for a human.

Full picture, including how to approve and revert: `docs/autonomy.md`.

## Where things live

| Thing | Path |
|---|---|
| Limits, universe, trading mechanics, schedules | `config/earn.yaml` (via `ops.config.load_config()`) |
| Model routing: auth, providers, per-task chains, budgets | `config/models.yaml` (via `ops.models_config.load_models_cfg()`) |
| Generated bot configs (committed, always `dry_run`) | `config/freqtrade-{a,b}.json`, `config/riskgate.json` — regen: `python -m ops.gen_freqtrade_config` |
| Machine-local runtime overlays (gitignored, mounted read-only) | `var/runtime/freqtrade-<s>.mode.json`, `var/runtime/runtime-<s>.json` (`$EARN_RUNTIME`), `var/runtime/compose.override.yml` |
| Tier-1 sleeve params | `config/params-sleeve-{a,b}.json` |
| Per-sleeve mode | `var/state/mode.json` — HMAC-signed, read via `ops.lib.mode_state.load()`, which **never raises**; `EarnConfig.phase` is computed from it and `earn.yaml` has no `phase:` key |
| Protected-config digest | `var/state/config.bless.json` via `ops.lib.config_guard` |
| Knowledge DB / journal DB (`SCHEMA_VERSION = 3`) | `knowledge/earn.db` / `journal/journal.db` (DDL: `ops/sql/*.sql`, migrations in `ops/sql/migrations/`) |
| The DB copy a **study** reads (never the live files) | `var/snapshots/<stamp>/{knowledge,journal}.db` + `manifest.json` — written hourly by `runs/snapshot_job.py`, read via `ops.lib.snapshot.latest(max_age_h=...)` |
| Market state | `knowledge/state/latest.json` (computed by code) |
| Data freshness the gate reads | `knowledge/state/freshness.json` (written by `ops.lib.freshness`) |
| Flags the gate reads | `knowledge/flags.json` (written ONLY through `ops.lib.flags`) |
| Proposals | `proposals/YYYY-MM-DD-HHMM.json`; shadow under `proposals/shadow/`; signed approvals under `proposals/approved/<run_id>.json` |
| Signal pipeline | `runs/signals/` — `scan`, `validate --signal-id`, `resolve`; evidence packs in `journal/snapshots/signals/<id>/pack.json` |
| LLM layer | `runs/llm/` — `chain.run_task(task, prompt, …)`; `router.resolve()` is the v1 view for legacy callers |
| Stage prompts | `prompts/stages/{flags,brief,brief_short,classify,scan,validate}.v1.md`, read through `ops.config.stage_prompt(cfg, stage)` |
| Kill switch | `ops/killdir/KILL` — presence = engaged. Written by a human, `POST /api/kill`, or `ops/healthcheck.py` on a mode mismatch, always through `ops.lib.kill`; removed only by a human. **What it means: no new risk, and no order whose size came from somewhere else** — stops, the daily flatten, a force-exit and the price-sized TP ladder still sell; the ensemble trim and SleeveB's proposal-driven sells do not (`docs/design/decisions-2026-09-30.md` §1) |
| The one operations lock | `ops/locks/ops.lock` via `ops.lib.oplock` (waits, bounded). Per-job non-blocking locks are `ops.lib.locks` / cron's `flock -n ops/locks/cron-<job>.lock` |
| Console | `console/` — `python -m console --port 8765 serve`; surface pinned in `docs/contracts.md` §9 |
| Generated ops files | `ops/crontab`, `ops/systemd/*.service` — generated by `ops/gen_ops_files.py`, never hand-edited |
| Candles (feather) | `data/binance/<PAIR>-<tf>.feather` (shared with the containers) |
| Worktrees for review sessions | `../earn-worktrees/<kind>-<key>` off `git.live_branch` (`runs/worktree.py`) |
| Reports | `reports/daily/<day>.md`, `reports/review-<week>.md`, `reports/earn.xlsx`, `reports/trace/` |

Never build a path under `var/` by hand — `ops.lib.paths` has a helper for each one, and it
honours `$EARN_STATE_ROOT` so a worktree session writes to the live data root.

## Conventions

- Timestamps: UTC ISO-8601 with `Z` (`2026-09-22T04:30:00Z`) in every file and DB row.
  Schedules are quoted in Gulf time (Asia/Dubai, UTC+4, no DST) only inside
  `config/earn.yaml: ops.schedules`, `research.slots` and the generated crontab.
- Sleeve identifiers are lowercase: `a`, `b`, `benchmark`.
- Research `run_id` format: `2026-09-22T08:30+04:00` (Gulf offset). Sleeve runs are
  `test-a-20261027-01` / `live-b-20270201-01`.
- Proposal target weights sum to 1 ± 0.001 — one epsilon everywhere.
- `schemas/proposal.py` is the ONE definition of the proposal shape;
  `strategies/proposal_loader.py` (stdlib-only, in-container) re-checks it structurally.
- Read DB columns by name, never by position: migrated and fresh databases differ in
  column *order* for tables that only gained columns. Every connection sets
  `row_factory = sqlite3.Row`.
- Writers use `ops.db.write()` (`BEGIN IMMEDIATE`, 3 retries, jittered backoff); readers
  use `ops.db.opened(path, readonly=True)` and close in a `finally`.
- Journal writers never raise into the bot loop; journaling failure must not veto or
  approve an order.
- Every job is idempotent and safe to rerun; cron wraps each in `flock` + `timeout` +
  `ops/envwrap.sh <job>`.
- New config keys need `description`, `x-tier` and `x-group` (use the `ops.config.F(...)`
  helper) or `tests/test_foundation/test_config_schema.py` fails. `x-effects` is declared
  on the section and inherited; a leaf may override it.
- A console feature router declares a **bare** area prefix (`APIRouter(prefix="/mode")`);
  `console/app.py` adds `/api` on mount. Adding an endpoint means adding a line to
  `docs/contracts.md` §9.6.

## Secrets

Never read `.env`, never echo a key, never print a token. Which job sees which variable is
decided by `ops/envwrap.sh` (`--print-allowlist <job>` prints names only), not by `.env`.

Model-facing jobs (`research`, `review`, `daily_review`, `maintenance`, `signals`,
`scanner`, `ingest`) carry a Claude credential and nothing else. `EARN_CLAUDE_AUTH_MODE`
decides which — `subscription` (the default, and what any unknown value means) gives
`CLAUDE_CODE_OAUTH_TOKEN` only and drops a present API key, because it would preempt
subscription auth in a headless run; `api_key` gives `ANTHROPIC_API_KEY` only; `auto` gives
the token plus the key renamed to `EARN_FALLBACK_ANTHROPIC_API_KEY`, a name the CLI cannot
pick up implicitly, so metered spend is always a deliberate, journaled attempt.

`EARN_CONSOLE_SECRET` and `EARN_CONSOLE_TOKEN` are in **no** allowlist (a test asserts it):
an automated run can neither sign a mode file nor log into the console. Binance keys are
allowlisted only for `reconcile` and `preflight`, and `runs/common.py: guard_env()` hard-fails
if one ever reaches a model job. Binance keys never appear in prompts, logs, journal rows or
chat.

## How to verify

```bash
.venv/bin/pytest -q                                   # unit suite (e2e deselected)
bash ops/smoke.sh                                     # end-to-end smoke, pass/fail summary
.venv/bin/pytest -m e2e -q                            # the same flows, without the bootstrap
.venv/bin/ruff check .                                # lint (never `ruff format`)
.venv/bin/python -m ops.gen_freqtrade_config --check   # generated-config drift
.venv/bin/python -m ops.gen_ops_files --check          # crontab + systemd unit drift
.venv/bin/python -m ops.check_gaps                    # candle continuity
cd console/web && npm run typecheck && npm run test:run && npm run build
```

## Further reading

`README.md` (what this is and how to run it) · `docs/console.md` (the 21 pages) ·
`docs/autonomy.md` (self-improvement) · `docs/signals.md` (the pipeline, providers, costs) ·
`docs/contracts.md` (the interfaces packages build against) · `/ops-runbook` (incidents).
