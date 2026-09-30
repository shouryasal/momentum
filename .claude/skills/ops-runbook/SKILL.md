---
name: ops-runbook
description: Step-by-step Earn operations runbook — restart the console or a bot, recover from a crashed transition or an exchange outage, rotate a credential, pause a sleeve or the whole system, resume after a monthly stop, restore a backup, use the kill switch, and untangle the ops lock. For manual invocation during incidents only.
disable-model-invocation: true
allowed-tools: Read, Bash(docker compose *), Bash(bash ops/*), Bash(python3 -m ops.*), Bash(.venv/bin/python -m ops.*), Bash(.venv/bin/python -m console *), Bash(.venv/bin/python -m runs.*), Bash(touch ops/killdir/*), Bash(rm ops/killdir/*), Bash(sqlite3 *), Bash(systemctl *), Bash(journalctl *)
---

# Ops runbook (tier 2 — human-only, manual `/ops-runbook` invocation only)

Every incident ends with a note in `knowledge/incidents/<date>-<slug>.md`: what happened,
when, root cause, fix, follow-up.

Run everything from the checkout on ext4 (`~/earn`), with `.venv/bin/python`. Prefer the
console when it is up — it audits what you did, takes the right lock and writes the rows.
This file is what you use when the console is the thing that is broken, or when you want
the underlying command.

**Where to look first:** console `/operations` (jobs, containers, host, logs, incidents),
`/invariants` (safety strip), `/audit` (who did what), `logs/<job>.log`.

---

## 0. Which lock is it? (read this before you kill anything)

Two locks coexist and they mean different things.

| Lock | Module | Behaviour | Held by |
|---|---|---|---|
| `ops/locks/ops.lock` | `ops.lib.oplock` | repo-wide, **waits** (30 s default), raises `OpsLockBusy` | mode transitions, test-run resets, config apply/regen/restart, crontab install, `apply_changes` merges |
| `ops/locks/cron-<job>.lock`, `ops/locks/{scanner,validate}.lock` | `ops.lib.locks` + cron's `flock -n` | per-job, **non-blocking** — a second copy just exits | one scheduled job at a time |

A `423 locked` from the console means the **ops** lock. Ask who has it:

```bash
.venv/bin/python -c "from ops.lib import oplock; print(oplock.is_held(), '|', oplock.holder())"
```

`flock(2)` dies with its holder, so a lock still held means a live process owns it. Find it
(`ps` on the pid in the holder line) and let it finish, or kill that process — never delete
the lock file while a process holds it. The console reports a lock still held at start-up
but never acts on it.

The kill switch never takes the ops lock. It always works.

---

## 1. Kill switch

* **Engage:** console `POST /api/kill` (any page's kill button), `/kill` on Telegram, or
  `touch ops/killdir/KILL`. The file is written **first**, then the bots are told, so a
  crash between the two still leaves the switch on.
* **What it does:** entries refuse immediately in the gate (`kill` is check 2 of 17);
  `ops.lib.kill.enforce` stops entries on each bot (`stopentry`, falling back to `stopbuy`)
  and cancels open entry orders; `ops/healthcheck.py` re-enforces within 5 minutes.
* **What it does to SELLS — one sentence:** *no new risk, and no order whose size came from
  somewhere else.* A reduction still fires when both the trigger and the size come from what
  the bot observes itself, or from you: the **stop-loss, the trailing stop, the daily-loss
  flatten, a force-exit, and the price-sized take-profit ladder all keep working.** What is
  **suspended** is every sell sized by a file another process wrote — SleeveA's ensemble trim
  and SleeveB's proposal-driven trims and `target_zero` closes — because KILL is engaged
  exactly when that file's provenance is in doubt (you said stop, or the healthcheck found a
  mode mismatch). Consequence to know: a position can drift above its cap during a KILL window
  while its own 200-day regime is still up. If you want it reduced, force-exit it; the machine
  will not decide to. Reversed on 2026-09-30 — `docs/design/decisions-2026-09-30.md` §1.
* **Runaway trading:** engage, then force-exit both sleeves (console `/mode` flatten, or
  `POST /api/bots/{sleeve}/forceexit`, or `/forceexit a all` on Telegram). Read
  `gate_decisions` before clearing anything.
* **Clear (human only):** `rm ops/killdir/KILL`. Healthcheck announces it. Nothing
  automated ever removes it.

---

## 2. Restart

### The console

```bash
sudo systemctl restart earn-console
sudo systemctl status earn-console
journalctl -u earn-console -n 100 --no-pager
```

Not installed as a unit, or you want it in the foreground:

```bash
.venv/bin/python -m console --port 8765 serve
```

Exit codes that are not bugs: **3** = no login token yet (`python -m console create-token`),
**3** = `EARN_AUTOMATED_RUN=1` is set in that shell (the console is human-only), **2** =
you passed a `--host` that is not `127.0.0.1`.

On start-up the console recovers by itself: a sleeve left in `ARMING`/`DISARMING` by a
crashed transition is stopped from entering and forced back to **TEST**; `queued`/`running`
backtest and job rows that survived the restart are failed rather than left as a lie. The
report is on `app.state.startup` and in the logs.

### A bot

Console → **Operations → Containers → Restart**, or `POST /api/bots/{sleeve}/restart`.
From a terminal, get the exact layered command for this machine (base compose plus the
generated live and credential layers, only the ones that exist):

```bash
.venv/bin/python - <<'PY'
from ops.config import load_config
from ops.lib.compose import Compose
c = Compose(load_config())
print(" ".join(c.argv("ps")))
print(" ".join(c.argv("restart", "freqtrade-a")))
PY
```

Then check it:

```bash
docker compose -p earn -f ops/docker-compose.yml logs --tail 50 freqtrade-a
```

Expect the dry-run banner in TEST and no tracebacks. **Do not** restart a bot with a bare
`docker compose -f ops/docker-compose.yml up -d` while a sleeve is live: that drops the
runtime overlay layers, so the bot loses its per-run database and its mode overlay.

If it flaps three times: stop it, check drift
(`.venv/bin/python -m ops.gen_freqtrade_config --check`), regenerate
(`.venv/bin/python -m ops.gen_freqtrade_config`), start it again.

### Telegram

```bash
sudo systemctl restart earn-telegram
journalctl -u earn-telegram -n 50 --no-pager
```

### A scheduled job, now

Console → **Operations → Jobs → Run now** (it spawns a detached process under the same
`flock` + `timeout` + `envwrap` wrapper the cron line uses and writes a `console_jobs` row).
The terminal equivalent, for a job whose cron line you can read in `ops/crontab`:

```bash
bash ops/envwrap.sh ingest -- .venv/bin/python -m runs.ingest
```

---

## 3. Recover

### A crashed or stuck mode transition

The console does this for you on start-up (section 2). To force it while it is running:
`GET /api/mode/recover`, or restart the console. To go down from a terminal — the only
break-glass path, and it only ever reaches TEST:

```bash
.venv/bin/python -m console set-mode --sleeve a --test
.venv/bin/python -m ops.gen_freqtrade_config        # rewrite var/runtime/*
```

Then restart that sleeve's container. There is **no** flag anywhere that reaches a live
state; going live is a console transition with a preflight, a typed phrase and step-up.

A transition that failed mid-flight rolled itself back and engaged KILL. Read
`mode_transitions.steps_json` (or the `/mode` history) to see which of the 12 steps failed,
fix that, clear KILL, and start again.

### Exchange outage

1. The bot pauses itself on ccxt errors and healthcheck alerts. **Do not touch keys.**
2. When Binance recovers, confirm orders reconcile: the bot's REST `/status` against the
   exchange's order history, and let the 15-minute reconcile job run
   (`.venv/bin/python -m runs.reconcile_job`). A mismatch above
   `risk.reconcile.tolerance_pct` raises the `reconcile_mismatch` flag, which is gate check
   7 and blocks entries until it clears.
3. Resume is nothing — the bot resumes. Note the incident.

### Model or provider outage

Usually nothing to do: the chain falls through on its own and every move is a
`provider_switches` row on the **AI & Models** page. Sleeve B keeps its last valid targets
and follows Sleeve A after 48 h. If a provider is stuck behind an open circuit breaker
after the cause is fixed, reset it from the AI & Models page
(`POST /api/llm/providers/{key}/circuit/reset`).

Ollama unreachable from WSL is almost always the NAT networking problem, not Ollama: see
the AI & Models page, which prints the exact PowerShell for this machine, or `README.md`.

### `database is locked`

Writers already use `BEGIN IMMEDIATE` with retries and a 5-second busy timeout, so a
persistent lock means a process is holding a write transaction open. Find it, stop it, and
check integrity before trusting the file:

```bash
sqlite3 journal/journal.db "PRAGMA integrity_check;"
sqlite3 knowledge/earn.db  "PRAGMA integrity_check;"
```

Never move a `.db` while `-wal`/`-shm` files exist beside it. If the checkout has somehow
ended up on `/mnt/c`, that is the cause — WAL locking does not work on 9p/drvfs. Move it to
ext4 and run `bash ops/setup.sh`.

### Missing candles

```bash
.venv/bin/python -m ops.check_gaps      # reports the gaps
bash ops/bootstrap_data.sh              # refetches the missing timerange, then re-checks
```

### A review session left a mess

Review and daily sessions work in `../earn-worktrees/<kind>-<key>` off `git.live_branch`,
never in the live checkout, and only `runs/apply_changes.py` writes the live HEAD. So the
live tree is safe by construction. List and prune:

```bash
git worktree list
git worktree prune
```

Worktrees older than `git.worktree_ttl_days` (14) are pruned by the maintenance job. A
worktree's **branch survives** a prune on purpose — the merge gate still needs its commits.
A change whose cherry-pick conflicted is **held**, never rejected; it stays in the queue on
`/self-improvement` for you.

---

## 4. Rotate

### Console login token

```bash
.venv/bin/python -m console rotate-token     # prints the PATH, never the token
cat ~/.config/earn/console-token
```

Only the salted hash is stored (`var/state/console_auth.json`). Existing sessions are
invalidated. The console API's own `POST /api/auth/rotate-token` likewise returns the path,
never the value.

### Suspected exchange-key compromise

1. **DELETE the key in the Binance console first.**
2. `touch ops/killdir/KILL`.
3. Create a new key: **Enable Reading + Enable Spot & Margin Trading only**, withdrawals
   **off**, IP-restricted. Put it in `.env` (0600).
4. Recreate the containers so the credential layer is re-read — use the layered command
   from section 2 with `up -d --force-recreate`.
5. Run a reconcile, clear KILL, write the incident note.

Keys rotate every 90 days regardless.

### Claude credential

```bash
claude setup-token          # new OAuth token -> CLAUDE_CODE_OAUTH_TOKEN in .env
```

Or change the auth mode on the console's **Secrets** page, which keeps
`config/models.yaml: auth.claude_mode` and `.env: EARN_CLAUDE_AUTH_MODE` in sync. Check what
a job would actually get — names only, never values:

```bash
bash ops/envwrap.sh --print-allowlist research
bash ops/envwrap.sh --print-env research
```

### `EARN_CONSOLE_SECRET` / `EARN_APPROVAL_KEY`

Rotating `EARN_CONSOLE_SECRET` invalidates the signed `var/state/mode.json` **and** the
config bless digest, so every sleeve immediately reads TEST (fail closed, by design). After
changing it:

```bash
.venv/bin/python -m console bless-config --reason "secret rotation"
```

then re-do any live transition through the console. Rotating `EARN_APPROVAL_KEY`
invalidates every outstanding approval file in `proposals/approved/`; re-approve from the
console.

---

## 5. Pause

| Scope | How |
|---|---|
| One sleeve's entries | `POST /api/bots/{sleeve}/stopentry`, or `/pause a` on Telegram. Entries stop, exits continue. Resume: restart that container (`initial_state: running`). |
| One sleeve, properly | Console `/mode` → transition that sleeve to **TEST**. Choose flatten-or-leave when disarming. |
| Everything, now | The kill switch (section 1). |
| Self-improvement | `autonomy.tier1_auto_merge: false` on `/self-improvement`. Every tier-1 change becomes **held**; nothing is lost. |
| The signal pipeline | `signals.planner.enabled: false` — scan and validate still run and the funnel still fills, but no research run is ever fired. `signals.enabled: false` stops it entirely. |
| A scheduled job | Comment is not an option: `ops/crontab` is generated. Disable the schedule in `config/earn.yaml: ops.schedules`, then `.venv/bin/python -m ops.gen_ops_files --write` and `--install --yes`. |

---

## 6. Resume after a MONTHLY stop (−10 %)

Only after the review run has root-caused the loss. The gate never clears this itself.

Preferred: console **`/risk` → Resume-after-monthly-stop wizard** (typed phrase +
step-up) — it audits the reason. The underlying state, if you must:

```bash
sqlite3 journal/journal.db \
  "DELETE FROM risk_state WHERE sleeve='b' AND key LIKE '%monthly_locked%';"
```

Risk state is namespaced `run:<run_id>:` per run, which is why the `LIKE` — check what is
there first with a `SELECT`. Then restart that sleeve's container.

---

## 7. Restore from a backup

```bash
bash ops/restore.sh 2026-10-27 --yes
```

It refuses without `--yes`, then: engages KILL → `docker compose down` → copies the backup
databases over the live paths and integrity-checks each → copies the file targets back →
brings the bots up. **Remove `ops/killdir/KILL` yourself afterwards**, once you have
checked the restored state.

Backups run at 03:00 (`bash ops/backup.sh`, i.e. `.venv/bin/python -m ops.backup`):
integrity-checked SQLite online copies plus the config and state files, retention
`backup.keep_daily` (14) / `backup.keep_weekly` (8), an optional best-effort mirror. Test
the destination from **Operations → Backups**, or back up now from the same tab.

Rehearse a restore twice before going live (G1).

---

## 8. Health and drift checks

```bash
.venv/bin/python -m ops.healthcheck                  # the 5-minute watchdog, on demand
.venv/bin/python -m ops.gen_freqtrade_config --check  # committed bot configs vs earn.yaml
.venv/bin/python -m ops.gen_ops_files --check         # crontab + systemd units vs config
.venv/bin/python -m ops.init_dbs                     # idempotent; brings schema to head
bash ops/setup.sh --check                            # host: ext4, dirs, .env, venv, git
```

Config edited by hand instead of through the console? Re-sign the digest, or preflight
check 3 will block going live:

```bash
.venv/bin/python -m console bless-config --reason "why"
```

Reconstruct any decision end to end:

```bash
.venv/bin/python -m runs.trace <run_id>              # writes under reports/trace/
```

---

## 9. Things that are deliberate, not faults

* Every sleeve reads **TEST** after `var/state/mode.json` goes missing, unsigned or
  unverifiable, and after `EARN_CONSOLE_SECRET` changes. Fail closed.
* The committed `config/freqtrade-*.json` always say `dry_run: true`. Live-ness comes from
  `var/runtime/`, never from a committed file, so no config save can move a bot live.
* The console refuses to start under `EARN_AUTOMATED_RUN=1` and returns `503` from every
  mutating route.
* A change whose evidence cannot be recomputed is **held**, not merged and not rejected.
* systemd units are only *staged* by `.venv/bin/python -m ops.gen_ops_files --install`; the
  console is not root, so it prints the `sudo` lines for you to run.
* `ops/crontab` and `ops/systemd/*.service` are generated. A hand edit is drift and a test
  will say so.
