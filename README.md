# Earn — autonomous crypto trading system

Earn is a spot-only, crypto-only trading system that runs unattended on your own
hardware: **Freqtrade executes, Claude analyses, and a deterministic risk gate sits
between them**. Two sleeves trade in parallel — Sleeve A (rules: 200d-MA trend filter,
volatility targeting, DCA) and Sleeve B (trades Claude's schema-validated proposals) —
benchmarked against buy-and-hold BTC. The system paper-trades for 90 days before any
live money, then goes live in propose mode (Telegram approve/reject) before execute mode.

Phase status: **paper** (see `config/earn.yaml: phase`).

Read [CLAUDE.md](CLAUDE.md) first — it holds the standing rules, the tier boundaries
(what Claude may change, what is human-only) and the conventions every run follows.

## Setup (WSL2 / Linux)

The repo must live on the WSL2 ext4 filesystem (e.g. `~/earn`), **never under `/mnt/c`**
— 9p mounts break SQLite WAL locking.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env && chmod 600 .env    # fill in keys; ops/.env is a symlink to it
ln -sf ../.env ops/.env
pre-commit install
```

## Claude sign-in (subscription-first)

The model-facing jobs (research, nightly + Sunday review, Monday maintenance,
the news classifier) authenticate with your **Claude Max subscription**, not a
metered API key:

```bash
claude setup-token          # sign in via browser; prints a long-lived token
# put the token in .env:
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
```

`ANTHROPIC_API_KEY` in `.env` works as a fallback, but when both are present
`ops/envwrap.sh` deliberately strips the API key from model-facing jobs — a
present API key would otherwise preempt subscription auth in headless runs.
USD figures in the journal are estimates (telemetry); degradation keys on the
subscription's rate-limit signals, and decision runs are never throttled.

Verify after the first live research run (`bash ops/envwrap.sh research -- \
.venv/bin/python -m runs.research_run`):

```sql
-- journal/journal.db: subscription auth + max reasoning effort applied
SELECT run_id, stage, effort, auth_source FROM runs WHERE stage='decide';
--                          -> effort='max', auth_source='none' (none = subscription)
```

Then watch for the first `reports/daily/<day>.md` (21:30 Gulf nightly review)
and the first Monday-02:00 maintenance row (`SELECT * FROM runs WHERE
stage='maintenance'`).

## Week-1 verification gate

Every command must exit 0, twice in a row:

```bash
pre-commit run --all-files
.venv/bin/pytest tests/test_foundation -q
.venv/bin/python -m ops.init_dbs && .venv/bin/python -m ops.init_dbs   # idempotent
.venv/bin/python -m ops.gen_freqtrade_config --check
cd ops && docker compose up -d && docker compose ps && cd ..           # both running
docker compose -f ops/docker-compose.yml logs freqtrade-a | grep -i "bot"
bash ops/bootstrap_data.sh                                             # candles, gap-free
cd ops && docker compose down && cd ..
```

## Layout

| Path | What |
|---|---|
| `config/earn.yaml` | Single source of truth for limits, universe, schedules (tier 2, human-only) |
| `config/freqtrade-*.json`, `riskgate.json`, `params-sleeve-*.json` | Generated from earn.yaml (`ops/gen_freqtrade_config.py`) |
| `strategies/` | Risk gate + SleeveA/SleeveB freqtrade strategies |
| `knowledge/` | Market data, news, state, flags (SQLite + files) |
| `journal/` | The system's own record: proposals, gate decisions, orders, fills, NAV |
| `proposals/` | Claude's schema-validated target-weight proposals |
| `runs/` | Scheduled jobs: ingest (+triggers), TCA, NAV (+what-if), research, daily/weekly review, maintenance, apply-changes, trace |
| `ops/` | Docker, cron, healthcheck, Telegram, backup, kill switch (`ops/killdir/KILL`) |
| `.claude/skills/` | The eleven skills the Claude runs load |
| `reports/trace/` | `python -m runs.trace <run_id>` — any decision reconstructed end-to-end |
