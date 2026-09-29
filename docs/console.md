# The console — a tour of all 21 pages

The console is the operator's seat. It is a FastAPI app serving a React build, bound to
`127.0.0.1` only (a code `Literal` in `console/settings.py`, not a setting), reachable at
<http://127.0.0.1:8765/>.

```bash
.venv/bin/python -m console create-token    # once; prints the path, never the token
cat ~/.config/earn/console-token            # read it yourself
.venv/bin/python -m console --port 8765 serve
.venv/bin/python -m console print-url       # URL + whether a token exists
```

Things that are true on every page:

* **Nothing dangerous happens without step-up.** A login gives you a session cookie
  (`earn_session`, HttpOnly, SameSite=Strict) for `console.session_hours`. Dangerous
  actions additionally need a **step-up** re-auth good for `console.stepup_minutes`
  (default 10), and the truly irreversible ones also need a **typed phrase**.
* **No response ever carries a secret value.** Secret-shaped facts are reported as
  `present`, a `last4`, or a path. Three independent checks enforce it: a static scan over
  every response model, a live crawl of every GET route against planted sentinels, and
  body redaction on the way out.
* **Live updates arrive over SSE** at `GET /api/stream` (`{topic, id, ts, payload}` frames,
  15-second heartbeat, replay by `Last-Event-ID`). Topics: `alert health kill mode
  transition bot nav order fill gate signal validation run proposal approval
  provider_switch config change job reconcile backtest` plus `log:<name>`.
* **The header** carries a per-sleeve mode badge (`A: TEST · seed 10,000`) and a health dot
  derived from `GET /api/ops/health` — stale data fails, open incidents or undelivered
  alerts warn, a missing number is `unknown` and never `ok`.
* **Ctrl-K** opens a command palette over pages, config fields, runs, signals, changes,
  skills and prompts (`GET /api/search`).
* **The console refuses to run under `EARN_AUTOMATED_RUN=1`** — it will not start, and
  every mutating route returns `503 automated_run`. An unattended Claude run can never
  reach it.

Errors use one envelope everywhere: `{"error": {"code", "message", "detail"}}`. The codes
you will actually meet: `423 locked` (someone else holds the ops lock), `409 conflict`
(the file changed under your edit), `403 step_up_required`, `503 unavailable` (a database
or config file is not there yet).

---

## Trading

### 1. Overview — `/`

The morning page. NAV cards per sleeve, a NAV-vs-BTC chart, exposure against the caps, the
gate's activity today, a 24-hour signal funnel, the last research run, the next scheduled
jobs, open incidents, active flags, provider usage and a "what changed today" strip.

Empty on a fresh install — that is correct, not broken.

### 2. Portfolio — `/portfolio`

Per sleeve: positions with weight vs cap, entries used out of `risk.max_entries_per_trade`,
the current stop level, trailing state, the next take-profit rung, and a **SIM badge** on
anything from a dry-run. Orders with a cancel button, fills with fee and
slippage-vs-decision-quote in bps, and a ledger-vs-exchange wallet panel showing the
reconciliation status.

### 3. Performance — `/performance`

NAV vs BTC with a drawdown underlay, sleeve and resolution switches, headline stat cards
with excess return, the metrics table, attribution by gate tag, and a proposals-only
what-if curve (what you would have made had every proposal executed perfectly). The TCA
caveat banner sits on top: in TEST these numbers have not paid real slippage.

### 4. Test Lab — `/test-lab`

Where you live for the first 90 days. An active-run card per sleeve with headline metrics
and the measured TCA caveat; a "reset required" banner when a pending seed change has not
been applied; the **reset wizard** (seed, label, notes, typed `RESET`, step-up), which
closes the current `sleeve_runs` row and opens a new one with a fresh per-run Freqtrade
database; run history with compare checkboxes; and a 2–5 run overlay with NAV rebased to
100, a metric-delta table and the config changes between the runs.

### 5. Backtest Lab — `/backtest-lab`

A form for a new backtest or walk-forward with a JSON config patch and TCA-measured cost
defaults; the queue table with per-window progress over SSE and a cancel button; a result
drawer with the window table and the patch that produced it. The queue is in-process and
single-worker — a console restart fails whatever was in flight, deliberately, because half
a walk-forward is worse than none.

### 6. Charts — `/charts`

Candles from the shared feather store with a moving-average overlay and fill, gate-reject
and proposal markers (faded when simulated), plus the proposal target table.

---

## Intelligence

### 7. Signals — `/signals`

The scanner funnel with conversion rates, a filterable signal list, and a detail drawer
showing the feature table, the screener's provider/model/rationale, the validator's
verdict, thesis, counter-evidence, invalidation condition and evidence-pack path, the
linked research run and proposal, and the resolved outcome. Buttons: **Scan now**,
**Revalidate**, **Inject manual signal**, **Mark noise**. Per-detector and per-model hit
rates sit alongside. See [signals.md](signals.md) for the pipeline itself.

### 8. Decisions — `/decisions`

Research runs with the model **requested vs served**, provider, switches with their
reasons, escalation reasons, effort, auth source, cost and the triggering signal; the
proposals timeline as stacked target weights; the approvals queue with countdowns; and a
run drawer with the rendered trace.

### 9. Risk — `/risk`

The limits table, churn / turnover / fee-budget meters, the `risk_state` anchors,
progress-to-stop bars, the flags panel (read-only), the effective trading mechanics
(read-only, deep-linked into Settings), and the gate-decision log with a severity filter
and a checks matrix. Also the **resume-after-monthly-stop wizard** (typed phrase +
step-up) — the gate never clears a monthly lock itself.

### 10. AI & Models — `/ai-models`

Provider cards with circuit-breaker state and a reset button; **Ollama detection** with
the WSL→Windows PowerShell guidance rendered for your machine and a model-pull button; the
routing matrix showing each task's chain, the code tier floors and where an overlay
changed something; usage grouped by task / model / provider / auth / day; the switch log;
and a playground that runs one prompt with tools forced off.

### 11. Skills — `/skills`

The skill table (status, bindings, policy, tests, origin, last change); a file-tree editor
with tier badges and server-side lint; Lint / Test / Eval / Trial buttons; the new-skill
wizard; archive and restore. `ops-runbook` is human-only end to end and shows as such.

### 12. Prompts — `/prompts`

Prompt families and versions with active/immutable badges; a CodeMirror editor that saves
the **next** version rather than overwriting the active one; a side-by-side diff; a render
preview with a token estimate against the task budget; the snapshots that used a version;
and step-up-gated activation.

### 13. Self-Improvement — `/self-improvement`

The change queue with counts and a detail drawer showing **claimed vs verified evidence**
with mismatches in red, the checks list, the git diff and the event timeline.
Approve / Reject / Revert / Attach. Below it: the autonomy matrix editor with the
auto-revert settings, the merge/revert timeline, and the recurring root causes. See
[autonomy.md](autonomy.md).

### 14. Knowledge — `/knowledge`

Briefs by date; the news explorer with corroboration status, the model-vs-rule label and
the claim check; source reliability; market state and regime history; asset dossiers;
incidents; and the reports index. Markdown is shown as preformatted text, not rendered
HTML.

---

## Control

### 15. Settings — `/settings`

The whole of `config/earn.yaml` and `config/models.yaml` as a form **generated from the
pydantic JSON Schema**, so a new config field appears here with no frontend change. A
section tree on the left; a Raw YAML tab; a History tab with revert; a "Field tools" panel
with per-field blame (actor, timestamp, reason), the schema default and a revert-to-default
button; a preview drawer with a real before/after diff; and a save dialog that lists the
**effects** of the save.

Effects come from the schema's `x-effects` and are inherited from the nearest annotated
ancestor: `regen` (regenerate the committed bot configs), `restart:freqtrade-a` /
`restart:freqtrade-b`, `crontab`, `restart:telegram`, `restart:console`, `reset_required`.
`console.effects_default` decides whether they apply immediately or are queued.

Comments in your YAML survive a scalar edit byte for byte. A structural edit (adding or
removing a key, replacing a block) falls back to a re-emit that keeps every comment but may
normalise flow style — the preview flags this before you save.

Anything marked `x-protected` (all of `risk.*`, `trading.*`, `universe.*`, `modes.live.*`,
`bounds.*`, `autonomy.*`, `security.*`, `git.*`, `console.*`, `runtime.*`, `exchange.*`,
`paths.*`, `sleeves.*.strategy`) needs step-up plus a typed confirmation and changes the
**bless digest** — the signed hash of the protected config that preflight check 3 verifies.

### 16. Setup wizard — `/setup`

Nine first-run steps, described in the README: Claude auth, seeds, backups, research slots,
paper anchor + live branch, Telegram, Ollama, host readiness, week-1 gate. Every step
writes through the same audited config path as Settings. The last step runs the healthcheck
job and prints the job id and log path.

### 17. Mode & Live — `/mode`

The page that decides whether real money moves. Per-sleeve state diagram
(`TEST → ARMING → LIVE_PROPOSE → LIVE_EXECUTE → DISARMING`), a target radio, a seed input
capped by `modes.live.max_seed_usdt`, the preflight checklist with per-item evidence and a
typed-reason override for the overridable ones, the typed confirmation
(`GO LIVE {sleeve} {seed} USDT`) and step-up gate, the 12-step transition progress streamed
over SSE, a flatten-or-leave choice when disarming, and the transition history with
rollback.

A `preflight_id` is valid for 10 minutes and the transition re-runs preflight before
acting. If any verification step fails the transition rolls back and engages KILL.

The break-glass path **down** is a terminal command, and it only ever goes to TEST:

```bash
.venv/bin/python -m console set-mode --sleeve a --test
```

### 18. Secrets — `/secrets`

Grouped rows showing presence and a `last4` with the jobs that use each secret;
write-only inputs behind step-up; the auth-mode selector (subscription / API key / auto)
with the billing warning and a `models.yaml` ↔ `.env` sync check; and a per-row credential
probe. Nothing here ever displays a secret, and
`POST /api/auth/rotate-token` returns the token's **path**, not the token.

---

## System

### 19. Operations — `/operations`

A KPI row (host readiness, data age, next job, open incidents) over tabs:

* **Jobs** — every `ops.schedules` entry, its recent runs, and **Run now**, which spawns a
  detached process under the same `flock` + `timeout` + `envwrap` wrapper the cron line
  uses, and writes a `console_jobs` row.
* **Host readiness** — the `ops/windows/check-host.ps1` facts through WSL interop: ext4,
  OneDrive, sleep-on-AC, the keep-alive task, timezone, Docker, disk — plus `host_sleep`
  (`ops.hostcheck`): whether the machine actually slept this week, in the sentence "this
  host slept for X hours in the last 7 days; unattended trading is not possible on it as
  configured", never blocking (see `docs/design/unattended-hosting.md`).
* **Crontab & units** — the rendered crontab versus the installed one, with a
  step-up-gated install. systemd units are **staged** with the `sudo` lines printed,
  because the console is not root.
* **Containers** — status and restart.
* **Backups** — test the destination, back up now.
* **Incidents + DB stats** — open incidents with a close button, database sizes and
  schema version.
* **Logs** — a redacted viewer over `logs/` with a follow mode.

### 20. Audit — `/audit`

One timeline merged from six tables — `audit_log`, `config_audit`, `mode_transitions`,
`proposal_approvals`, `change_events`, `console_jobs` — normalised to a single shape
(`ts_utc, source, actor, action, target, result, detail`), with filters and a diff drawer.
Actors are always one of `human:console:<sid>`, `human:cli`, `human:telegram` or
`system:<job>`; a refusal is a row too (`result='denied'`).

### 21. Invariants — `/invariants`

The safety strip, and one row per invariant with its status, its evidence and the exact
`file:function` that enforces it. These are **code, not config** — nothing on this page is
editable, and that is the point:

| Invariant | Enforced by |
|---|---|
| Console binds 127.0.0.1 only | `console/settings.py` |
| `EFFORT_FLOOR = "high"` | `runs/router.py:clamp_effort` |
| `ALWAYS_DISALLOWED` tools | `runs/decision_core.py` |
| Stop exits are market orders | `strategies/earn_base.py` |
| `decide` needs tier ≥ 4, `validate` tier ≥ 3 | `runs/llm/types.py:MIN_TIER_FLOOR` |
| No local model writes a proposal | `runs/llm/types.py:chain_for` |
| Committed bot configs are always `dry_run` | `ops/gen_freqtrade_config.py:build_bot_config` |
| Unverified mode state ⇒ TEST | `ops/lib/mode_state.py:load` |
| Mode writes refuse under `EARN_AUTOMATED_RUN=1` | `ops/lib/mode_state.py:write` |
| The kill switch never waits for the ops lock | `ops/lib/kill.py:engage_and_enforce` |
| Tier-2 paths | `.claude/hooks/tier2_paths.py` |

The page also lists the last hook denials, so you can see the tier-2 guard actually firing.

---

## The HTTP surface

Every route the console serves is pinned in [contracts.md](contracts.md) §9.6 and asserted
by `tests/test_console/test_route_inventory.py`. Adding an endpoint means adding a line
there. The machine-readable schema is at `GET /api/openapi.json` (there is no Swagger UI —
`docs_url` is deliberately off).
