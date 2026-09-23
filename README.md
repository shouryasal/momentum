# Earn — an autonomous crypto trading system you run yourself

Earn is a spot-only, crypto-only trading system that runs unattended on your own laptop.
**Freqtrade executes, Claude analyses, a deterministic risk gate sits between them, and a
local web console is how you drive the whole thing.**

Two sleeves trade in parallel on Binance spot, BTC/USDT and ETH/USDT only:

* **Sleeve A** (`strategies/SleeveA.py`) — rules: 200-day MA trend filter, volatility
  targeting, scheduled DCA. No model in the loop.
* **Sleeve B** (`strategies/SleeveB.py`) — trades Claude's schema-validated target-weight
  proposals, after the gate has approved every order.

Both are benchmarked against holding BTC. Earn earns its place through smaller drawdowns
and discipline, not fast gains.

## The four moving parts

| Part | What it is | Where |
|---|---|---|
| **Console** | FastAPI + React on `http://127.0.0.1:8765`, 21 pages. Token login, signed session cookie, step-up re-auth for anything dangerous. It never returns a secret value and refuses to run inside an automated run. | `console/` — tour in [docs/console.md](docs/console.md) |
| **Bots** | Two Freqtrade 2026.8 containers (`freqtrade-a`, `freqtrade-b`) under Docker. | `ops/docker-compose.yml` |
| **Risk gate** | 17 deterministic checks in Freqtrade callbacks. Claude is never in the order path. | `strategies/riskgate.py` |
| **Claude/Ollama pipeline** | Provider-agnostic LLM layer: per-task ordered model chains, circuit breakers, automatic switching, every switch journaled. Claude Agent SDK (subscription **or** API key) and local Ollama are both first-class. | `runs/llm/` — see [docs/signals.md](docs/signals.md) |

Work flows scanner → validator → planner → gate → Freqtrade
([docs/signals.md](docs/signals.md)), and the system improves itself through a verified
change gate ([docs/autonomy.md](docs/autonomy.md)).

**Mode is not a config key.** Each sleeve's mode lives in the HMAC-signed
`var/state/mode.json` and is read through `ops.lib.mode_state.load()`, which fails closed
to TEST if the file is missing, unsigned or unverifiable. A config save can never move a
bot live; the committed `config/freqtrade-*.json` always say `dry_run: true`.

Read [CLAUDE.md](CLAUDE.md) for the tier boundaries (what Claude may change, what is
human-only) and [docs/contracts.md](docs/contracts.md) for the interfaces the packages
build against.

---

## First run, exactly

The target host is Windows 11 with **WSL2 Ubuntu 24.04** and **Docker Desktop**. Python and
Node live inside WSL; only Ollama and the browser stay on the Windows side.

### 1. Clone onto the WSL filesystem

The checkout **must** be on ext4 inside WSL — `~/earn` is the convention. A copy under
`/mnt/c` (OneDrive, Documents, anywhere on the Windows drive) is a 9p/drvfs mount, where
SQLite WAL locking and git worktrees corrupt. `ops/setup.sh` refuses to continue there.

```bash
# inside WSL:  wsl -d Ubuntu-24.04
git clone <your remote> ~/earn
cd ~/earn
```

### 2. Run the host setup

```bash
bash ops/setup.sh              # idempotent: creates, never overwrites
bash ops/setup.sh --check      # report only, change nothing
```

In order it checks the filesystem, creates every runtime directory (`logs/`, `ops/locks/`,
`ops/killdir/`, `var/{state,runtime}`, `proposals/{pending,approved,shadow}`, …), copies
`.env.example` to `.env` at 0600 and symlinks `ops/.env` to it, generates
`EARN_CONSOLE_SECRET`, `EARN_CONSOLE_TOKEN` and `EARN_APPROVAL_KEY` exactly once (never
printed, never regenerated), creates `.venv` and installs the project, initialises both
databases, and checks your git identity. It exits non-zero on any blocking problem.

### 3. Fill in `.env`

`ops/setup.sh` generated the three `EARN_*` secrets. You add the rest; `.env.example`
documents each one. Leave the Binance keys **empty** through the paper phase.

Which job sees which variable is decided by `ops/envwrap.sh`, not by `.env`:

```bash
bash ops/envwrap.sh --print-allowlist research    # names only, never values
```

### 4. Choose how Claude authenticates

Three modes, set by `EARN_CLAUDE_AUTH_MODE` in `.env` and `auth.claude_mode` in
`config/models.yaml` (the console's Secrets page keeps the two in sync):

| Mode | What a job gets | When |
|---|---|---|
| `subscription` (default) | `CLAUDE_CODE_OAUTH_TOKEN` only under that name. A present `ANTHROPIC_API_KEY` is **renamed** to `EARN_FALLBACK_ANTHROPIC_API_KEY`, never exported under the plain name that would preempt subscription auth in a headless run. | You have Claude Max. No metered spend unless something explicitly asks for the key. |
| `api_key` | `ANTHROPIC_API_KEY` only; the OAuth token is dropped. The one mode that exports the plain name. | No subscription. Metered, capped by `auth.api_key_monthly_cap_usd` (default \$30/month). |
| `auto` | The token (if any) **plus** the key renamed to `EARN_FALLBACK_ANTHROPIC_API_KEY`. | Subscription first, metered fallback only as a deliberate, journaled switch. |

The rename is unconditional in `subscription` **and** `auto`, token or no token:
`EARN_FALLBACK_ANTHROPIC_API_KEY` is a name the Claude CLI can never pick up implicitly,
so a key can only ever be spent through an explicit, journalled, capped `claude:api_key`
attempt. Anything unknown means `subscription`. For the subscription path:

```bash
claude setup-token          # browser sign-in; prints a long-lived OAuth token
# put it in .env as CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
```

### 5. Ollama, and the WSL→Windows networking fix

Ollama serves the two cheap tasks (`scan`, `classify`) and is the last fallback for
`brief` and `flags`. It can never serve `decide` or `validate` — that is a code floor
(`runs/llm/types.py: MIN_TIER_FLOOR`), not a setting.

Install Ollama on **Windows** and pull the model:

```powershell
ollama pull llama3.1:8b
```

Then the part that always bites: in WSL2's default NAT networking, `127.0.0.1` inside WSL
is **not** the Windows host, and Ollama binds `127.0.0.1` by default — so nothing in WSL
can reach it. Pick one fix (the console's AI & Models page prints these for your machine,
with your actual gateway address):

**Option A — bind Ollama to all interfaces** (works on any WSL mode; PowerShell as
Administrator):

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
Get-Process ollama* | Stop-Process -Force
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
New-NetFirewallRule -DisplayName "Ollama from WSL" -Direction Inbound `
  -Protocol TCP -LocalPort 11434 -Action Allow `
  -RemoteAddress 172.16.0.0/12,192.168.0.0/16
```

**Option B — mirrored networking** (Windows 11 22H2+; `127.0.0.1` becomes shared):

```powershell
Add-Content -Path "$env:USERPROFILE\.wslconfig" -Value "[wsl2]`nnetworkingMode=mirrored"
wsl --shutdown
```

Check from inside WSL:

```bash
curl -s --max-time 2 http://127.0.0.1:11434/api/version
curl -s --max-time 2 "http://$(ip route show default | awk '{print $3}'):11434/api/version"
```

Whichever answers is the base URL. Leave `providers.ollama.base_url: auto` in
`config/models.yaml` and Earn probes `127.0.0.1`, the WSL default gateway, the
`resolv.conf` nameserver and `host.docker.internal` in that order. An explicit URL must be
loopback or private — a public address is refused at load.

### 6. Initialise the databases and generate the ops files

```bash
.venv/bin/python -m ops.init_dbs                    # idempotent; schema version 3
.venv/bin/python -m ops.gen_freqtrade_config        # config/freqtrade-{a,b}.json, riskgate.json
.venv/bin/python -m ops.gen_ops_files --check       # crontab + systemd units match config?
.venv/bin/python -m ops.gen_ops_files --write       # rewrite them if they drifted
```

`ops/crontab` and `ops/systemd/*.service` are **generated** — never hand-edit them. Install
them once you are happy:

```bash
.venv/bin/python -m ops.gen_ops_files --install --yes
```

That writes your user crontab directly and **stages** the systemd units in
`var/runtime/systemd/` with the exact `sudo` lines to finish the job (the console user is
deliberately not root).

### 7. Build the console frontend and mint a login token

```bash
cd console/web && npm ci && npm run build && cd ../..   # builds into console/static/
.venv/bin/python -m console create-token                # prints the PATH, never the token
cat ~/.config/earn/console-token                        # read it yourself
```

### 8. Start the console and open it

```bash
.venv/bin/python -m console --port 8765 serve
# or, once the unit is installed:
sudo systemctl enable --now earn-console
.venv/bin/python -m console print-url
```

Open <http://127.0.0.1:8765/> in your Windows browser and paste the token. The console
binds `127.0.0.1` only — that is a code `Literal`, not a setting, and
`serve --host 0.0.0.0` is rejected.

### 9. Run the Setup wizard

Console → **Setup wizard** (`/setup`), nine steps, each writing real config through the
same audited path as the Settings page:

1. **Claude auth** — subscription / API key / auto, and the API-key monthly cap.
2. **Seeds** — starting balance per sleeve for the next TEST run.
3. **Backups** — destination (default `~/earn-backups`) and an optional mirror.
4. **Research slots** — the Gulf-time fire times (default `08:30`, `16:00`). This is the
   single source for the cron lines, the nearest-slot check and the healthcheck.
5. **Paper start & live branch** — where the paper record begins, and the branch the
   change gate may merge into.
6. **Telegram** — ops alerts and, later, live approvals.
7. **Ollama** — detection, with the networking guidance above and a model pull button.
8. **Host readiness** — ext4, sleep policy, keep-alive task, Docker, clock, disk.
9. **Week-1 gate** — runs the healthcheck job and shows its verdict with the log path.

### 10. Make the Windows host dependable

WSL2 is not a server: Windows suspends the VM when its last process exits, sleeps on AC,
and does not start WSL at boot. Any of the three stops the bots while the exchange keeps
trading. From an **Administrator PowerShell on Windows**:

```powershell
.\ops\windows\install-autostart.ps1                  # keep-alive task + no sleep on AC
.\ops\windows\install-autostart.ps1 -WriteWslConfig  # also suggest .wslconfig (backed up)
.\ops\windows\check-host.ps1                         # read-only report; -Json for the console
```

The live preflight blocks going live while standby-on-AC is non-zero or the keep-alive task
is missing.

### 11. The week-1 verification gate

Every command must exit 0, twice in a row:

```bash
pre-commit run --all-files
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m ops.init_dbs && .venv/bin/python -m ops.init_dbs   # idempotent
.venv/bin/python -m ops.gen_freqtrade_config --check                   # generated-config drift
.venv/bin/python -m ops.gen_ops_files --check                          # crontab + unit drift
bash ops/smoke.sh                                                      # end-to-end smoke
docker compose -p earn -f ops/docker-compose.yml up -d
docker compose -p earn -f ops/docker-compose.yml ps                    # both running
docker compose -p earn -f ops/docker-compose.yml logs freqtrade-a | head -50
bash ops/bootstrap_data.sh                                             # candles, gap-free
docker compose -p earn -f ops/docker-compose.yml down
```

`ops/bootstrap_data.sh` downloads 1h/4h/1d candles from 2021 for **every pair in the
current universe snapshot** — the ~100-name watchlist plus `universe.data_only_symbols`
(BNB, for fee conversion only; it is never tradeable) — and then runs `ops.check_gaps`. The
pair list is not hardcoded: it comes from `knowledge/universe/<date>.json`, which
`ops.universe_refresh` resolves weekly from the live Binance API and which is also the
whitelist the bots run and the backtests replay. Measured, that is ~0.15 GB and ~6 minutes.
On a reported gap, re-run the script; `download-data` refetches the missing range.

`ops/refresh_backtest_data.sh` (the Sunday-18:00 `backtest_data` job) refreshes the universe
first and then tops the candles up, because a name that entered the universe this week has
no history yet. A refused refresh — the tradeable tier would shrink past
`universe.refresh.max_tradeable_shrink`, or a core asset failed to resolve — stops the job
and leaves the previous snapshot in place.

---

## Running in TEST

TEST is the normal state and the one every sleeve fails closed to. It is **Freqtrade
dry-run on live market data** — real candles, real spreads, simulated fills:

* a per-run Freqtrade database at `ft_userdata/<sleeve>/runs/<run_id>.sqlite`, so resetting
  a test run never contaminates the last one;
* a UI-set seed per sleeve (`modes.test.seed_usdt`, default 10 000 each);
* run-scoped risk state — every gate anchor is namespaced `run:<run_id>:`;
* NAV measured against a BTC benchmark anchored at the run's start;
* P&L shown next to the **TCA-measured** cost caveat, because simulated fills do not pay
  real slippage.

Day to day you do nothing: cron drives it.

| When (Gulf) | Job | What |
|---|---|---|
| `*/5` | `runs.signals scan` | the scanner cycle |
| `*/5` | `ops.healthcheck` | container heartbeats, staleness, missed runs, KILL processing, alerts |
| `*/15` | `runs.ingest` | candles, books, funding, news → freshness sidecar |
| `*/15` | `runs.nav_tick` | ledger NAV points |
| `*/15` | `runs.reconcile_job` | ledger vs exchange (live only in effect) |
| `:05 hourly` | `runs.tca_job` | execution costs |
| `00:10` | `runs.nav_job` | daily NAV row |
| `08:30`, `16:00` | `runs.research_run <slot>` | the decision run → a proposal |
| `21:30` | `runs.daily_review` | nightly review in a git worktree |
| `Sun 20:00` | `runs.review_run` | weekly review in a git worktree |
| `Sun 18:00` | `ops/refresh_backtest_data.sh` | backtest data refresh |
| `03:00` | `ops/backup.sh` | integrity-checked SQLite backups + retention |
| `Mon 02:00` | `runs.maintenance` | housekeeping, shadow-model management |

Every line runs under `flock` (no overlap) + `timeout` (its `deadline_s`) + `envwrap.sh`
(its secret allowlist), and logs to `logs/<job>.log`. Watch the first
`reports/daily/<day>.md` appear after the 21:30 review, then check the Overview page.

To reset a test run: console → **Test Lab** → reset wizard (seed, label, notes, typed
`RESET`, step-up). To compare runs: tick 2–5 of them and read the overlay with NAV rebased
to 100.

Anything you need to reconstruct is one command away:

```bash
.venv/bin/python -m runs.trace <run_id>     # writes a markdown trace under reports/trace/
```

---

## What going LIVE requires

Live is per sleeve, it is never a config edit, and there is no CLI flag that reaches it.
The only path is console → **Mode & Live** → preflight → typed confirmation → step-up.

There are two live sub-modes:

* **`LIVE_PROPOSE`** — real orders, but every proposal needs a human approval first
  (Telegram or the console). The approval is an HMAC-signed file in
  `proposals/approved/<run_id>.json` that the in-container loader verifies, including the
  proposal's own sha256, so an approval cannot be replayed against a rewritten proposal.
  Approvals expire after `modes.live.approval_ttl_hours` (6).
* **`LIVE_EXECUTE`** — the bot acts on its own, still entirely inside the gate.

`POST /api/mode/preflight` runs **13 checks** (`ops/preflight.py: CHECK_ORDER`), and a
`preflight_id` is valid for 10 minutes:

| # | Check | Blocking |
|---|---|---|
| 1 | Kill switch clear, no blocking flags, data fresh | yes |
| 2 | Both bots healthy on a real strategy, gate active | yes |
| 3 | Config blessed, git clean, generated files in sync | yes |
| 4 | Exchange key present, spot-only, account not already live | yes |
| 5 | Seed within ceiling and covered by free balance | yes |
| 6 | Test track record and breach-free streak | yes |
| 7 | Telegram reachable (approvals and alerts depend on it) | yes |
| 8 | Exchange-side stops supported and enabled | yes |
| 9 | Host cannot sleep, docker up, clock and cron sane | yes |
| 10 | Automation cannot reach console or approval secrets | yes |
| 11 | Recent backup and a writable destination | no (warn) |
| 12 | `strategies` test suite green | no (warn) |
| 13 | Time in `LIVE_PROPOSE` with a high approval rate | yes (for `LIVE_EXECUTE`) |

The policy behind them lives in `config/earn.yaml: modes.live` — `min_test_days: 90`,
`min_propose_days: 30`, `require_zero_breach_days: 30`, `max_seed_usdt` per sleeve (a hard,
step-up-protected ceiling), `one_live_sleeve_per_account: true`,
`require_stoploss_on_exchange: true`, and `confirm_phrase: "GO LIVE {sleeve} {seed} USDT"`,
which you type verbatim.

The transition itself is 12 steps under the ops lock, streamed to the UI: stop entries,
cancel open entry orders, close the old `sleeve_runs` row, write the signed mode file,
regenerate `var/runtime/*`, recreate the container, verify `/show_config` (dry_run,
strategy, bot name, seed, db_url, `stoploss_on_exchange`), reconcile ledger vs exchange,
open the new run row, audit. Any mismatch rolls back and engages KILL.

Before you ever put real money in:

1. Binance keys with **Reading + Spot trading only**, withdrawals **off**, IP-restricted
   once you have a static address. Rotate every 90 days.
2. Rehearse `bash ops/restore.sh <YYYY-MM-DD> --yes` twice from a real backup.
3. Read `/ops-runbook` — restart, recover, rotate, pause, restore.

---

## Layout

| Path | What |
|---|---|
| `config/earn.yaml` | Single source of truth for limits, universe, trading mechanics, schedules (tier 2, human-only) |
| `config/models.yaml` | Model routing: auth mode, providers, per-task chains, budgets, switching (tier 2) |
| `config/freqtrade-*.json`, `riskgate.json`, `params-sleeve-*.json` | Generated from `earn.yaml` by `ops/gen_freqtrade_config.py`; committed and drift-checked |
| `console/` | The operator console — FastAPI (`console/`) + React (`console/web`, built into `console/static`) |
| `strategies/` | Risk gate, trading mechanics, SleeveA/SleeveB Freqtrade strategies |
| `runs/` | Every scheduled job, plus `runs/llm/` (the model layer) and `runs/signals/` (the pipeline) |
| `ops/` | Setup, Docker, generated crontab and units, envwrap, healthcheck, Telegram, backup, kill switch (`ops/killdir/KILL`), Windows scripts |
| `knowledge/` | Market data, news, computed state, flags (SQLite + files) |
| `journal/` | The system's own record: proposals, gate decisions, orders, fills, NAV, audit |
| `proposals/` | Claude's schema-validated proposals; `approved/` holds the signed approvals |
| `var/` | Machine-local, gitignored, 0700: signed mode file, bless digest, runtime overlays mounted read-only into the containers |
| `evals/`, `schemas/`, `changes/`, `prompts/` | Change verification, JSON schemas, the tier-1 change queue, prompt bodies |
| `.claude/skills/` | The skills Claude runs load (plus `_template/`, which is not one) |
| `reports/` | Daily and weekly reviews, TCA, backtests, `reports/trace/<run_id>.md` |

## Documentation

| Read | For |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Standing rules, tier boundaries, conventions — every Claude run reads it first |
| [docs/console.md](docs/console.md) | A tour of all 21 console pages |
| [docs/autonomy.md](docs/autonomy.md) | What the system may change by itself, and how to approve or revert it |
| [docs/signals.md](docs/signals.md) | The scanner → validator → planner → gate pipeline, providers, costs |
| [docs/contracts.md](docs/contracts.md) | The interfaces the packages build against (env vars, `var/`, DB, the route table) |
| `/ops-runbook` | Incident procedures — restart, recover, rotate, pause, restore |
| `docs/design/` | The original planning doc and build spec. Historical: where they disagree with the code, the code won and `docs/contracts.md` records it. |

## How to verify a change

```bash
.venv/bin/pytest -q                                  # unit suite (e2e deselected)
bash ops/smoke.sh                                    # end-to-end smoke, pass/fail summary
.venv/bin/pytest -m e2e -q                           # the same flows, without the bootstrap
.venv/bin/ruff check .                               # lint (never `ruff format`)
.venv/bin/python -m ops.gen_freqtrade_config --check  # generated-config drift
.venv/bin/python -m ops.gen_ops_files --check         # crontab + systemd unit drift
.venv/bin/python -m ops.check_gaps                   # candle continuity
cd console/web && npm run typecheck && npm run test:run && npm run build
```
