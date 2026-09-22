# CLAUDE.md — standing rules for every Claude run and session

## What this repo is

Earn: a spot-only crypto system on Binance (UAE), BTC/USDT and ETH/USDT only.
Claude analyses and proposes target weights; code decides whether that is allowed and
how to execute it. The benchmark is holding BTC — Earn earns its place through smaller
drawdowns and discipline, not fast gains.

## Non-negotiables

- Claude never sits in the order path. A deterministic risk gate (`strategies/riskgate.py`,
  Freqtrade callbacks) validates every order before Freqtrade sends it.
- Numbers in, numbers out: indicators are computed by Python and handed to Claude as
  numbers. Never estimate a price, an RSI or a volatility from memory.
- `abstain: true` is the default when inputs are stale or conflicting.
- All limits come from `config/earn.yaml` — never restate a limit by hand anywhere.

## Tier boundaries (spec §7)

| Tier | Who | Paths |
|---|---|---|
| 0 — free | Claude, every run | `knowledge/**`, `lessons.md` (append-only, dated), briefs, its own notes |
| 1 — gated | Claude, via `changes/*.json` + `runs/apply_changes.py` | `config/params-sleeve-{a,b}.json` (inside `bounds:` from earn.yaml), `prompts/**`, skill bodies of `decide`, `strategy-lab`, `market-state` |
| 2 — human only | Shourya, normal Claude Code session with tests | `config/**` (except params files), `strategies/**`, `runs/**`, `evals/**`, `ops/**`, `schemas/**`, `tests/**`, `.env*`, `.claude/settings.json`, `.claude/hooks/**`, skills `tca`, `risk-gate`, `ops-runbook` |

The PreToolUse hook (`.claude/hooks/protect_tier2.py`) enforces tier 2 in automated runs
(`EARN_AUTOMATED_RUN=1`); `runs/apply_changes.py` refuses to merge any commit touching a
tier-2 path. The pattern list lives in `.claude/hooks/tier2_paths.py` — the one source.

## Where things live

| Thing | Path |
|---|---|
| Limits, universe, schedules | `config/earn.yaml` (via `ops.config.load_config()`) |
| Generated bot configs | `config/freqtrade-{a,b}.json`, `config/riskgate.json` (regen: `python -m ops.gen_freqtrade_config`) |
| Tier-1 sleeve params | `config/params-sleeve-{a,b}.json` |
| Knowledge DB / journal DB | `knowledge/earn.db` / `journal/journal.db` (DDL: `ops/sql/*.sql`) |
| Market state | `knowledge/state/latest.json` (computed by code) |
| Flags the gate reads | `knowledge/flags.json` (written ONLY through `ops.lib.flags`) |
| Proposals | `proposals/YYYY-MM-DD-HHMM.json` (host-validated before write; shadow runs under `proposals/shadow/`) |
| Kill switch | `ops/killdir/KILL` — presence = engaged; human-created, human-removed |
| Candles (feather) | `data/binance/<PAIR>-<tf>.feather` (shared with containers) |
| Reports | `reports/earn.xlsx`, `reports/*-weekly.md`, `reports/review-<week>.md` |

## Conventions

- Timestamps: UTC ISO-8601 with `Z` (`2026-09-22T04:30:00Z`) in every file and DB row.
  Schedules are quoted in Gulf time (Asia/Dubai, UTC+4, no DST) only inside
  `config/earn.yaml: ops.schedules` and the crontab.
- Sleeve identifiers are lowercase: `a`, `b`, `benchmark`.
- `run_id` format: `2026-09-22T08:30+04:00` (Gulf offset).
- Proposal target weights sum to 1 ± 0.001 — one epsilon everywhere.
- Journal writers never raise into the bot loop; journaling failure must not veto or
  approve an order.
- Every job is idempotent and safe to rerun; cron wraps jobs in `flock` + `timeout`.

## Secrets

Never read `.env`, never echo keys. Research and review runs carry only
`ANTHROPIC_API_KEY` by construction (`ops/envwrap.sh` allowlists per job). Binance keys
never appear in prompts, logs, journal rows or chat.

## How to verify

```bash
.venv/bin/pytest -q                                # full suite
.venv/bin/python -m ops.gen_freqtrade_config --check   # generated-config drift
.venv/bin/python -m ops.check_gaps                 # candle continuity
```
