# Earn contracts — what F0 fixes and every other package builds against

`F0` lands first; `P1…P7` then run in parallel. This file is the interface between them.
If something here changes, it changes **here first** and the owning package updates with it.
Anything not listed is an implementation detail of the package that owns the file.

Ownership rule: a package edits only the files its work-package lists. A change it needs
elsewhere is either a trivially safe seam or a cross-package request.

This file describes the code that **shipped**, not the plan. Where the spec and the landed
code disagreed, the code won and the difference is called out where it matters (router
prefixes in §9.1, `models.yaml` v2 in §3, the `auto` auth mode in §7).

Packaging: `[tool.setuptools] packages` in `pyproject.toml` lists every importable package
— `console`, `console.routers`, `console.services`, `evals`, `ops`, `ops.lib`, `runs`,
`runs.llm`, `runs.llm.providers`, `runs.signals`, `schemas`, `strategies`. Tests import
from the rootdir, so a missing entry only bites a non-editable install;
`tests/test_foundation/test_packaging.py` compares the list against what is on disk.
Deliberately **not** dependencies, despite spec §5.1: `itsdangerous` (the session signer is
stdlib HMAC) and `sse-starlette` (the SSE body is hand-written).

---

## 1. Environment variables

| Variable | Who sets it | Who reads it | Notes |
|---|---|---|---|
| `EARN_STATE_ROOT` | `ops/envwrap.sh`, `console`, worktree sessions | `ops.lib.paths` (everything else goes through it) | The live data root. Unset = the checkout root. A review/daily session runs in a git worktree with this pointing at the live root. |
| `EARN_WORKTREE` | worktree sessions | `ops.lib.paths.in_worktree()` | The worktree path; presence means "this is not the live checkout". |
| `EARN_LIVE_ROOT` | worktree sessions | `runs/apply_changes.py` (P6) | Where the live checkout is, for merges. |
| `EARN_CONSOLE_SECRET` | the human / `ops/setup.sh` | `ops.lib.signing`, `ops.lib.mode_state`, `ops.lib.config_guard` | HMAC key for the mode file and the bless digest. In **no** `envwrap.sh` allowlist — a test asserts this. Absent ⇒ every sleeve reads TEST. |
| `EARN_CONSOLE_TOKEN` | the human / `ops/setup.sh` | `console/security.py` (F0b) | Console login token. In no allowlist. |
| `EARN_APPROVAL_KEY` | `ops/setup.sh` | `runs/approvals.py` (P5), the in-container loader | HMAC for proposal approvals. Allowlisted for the `telegram` job only. |
| `EARN_AUTOMATED_RUN` | `ops/envwrap.sh` | `ops.lib.paths.is_automated_run()`, `ops.lib.mode_state.write`, the console | `1` inside an unattended Claude run. Mode writes refuse; the console refuses to start and refuses every mutating route. |
| `EARN_RUNTIME` | `ops/docker-compose.yml` | the strategy (P2) | Path of `runtime-<sleeve>.json` inside the container. |
| `EARN_RISKGATE` | compose | `strategies/riskgate.py` | Path of `riskgate.json` inside the container (default `config/riskgate.json`). |
| `EARN_SLEEVE` | compose | `strategies/riskgate.py` | `a` or `b`. |
| `EARN_CLAUDE_AUTH_MODE` | `.env`, written by the console when `auth.claude_mode` is saved | `ops/envwrap.sh` (P1) | `subscription` \| `api_key` \| `auto`; unknown ⇒ `subscription`. |
| `EARN_FALLBACK_ANTHROPIC_API_KEY` | `ops/envwrap.sh` in `subscription` **and** `auto` mode | `runs/llm/providers/claude_sdk.py` (P3) | The API key under a name the CLI can never pick up implicitly. |

Constants that are **code, not config**: the console bind address `127.0.0.1`,
`EFFORT_FLOOR = "high"`, `ALWAYS_DISALLOWED`, market-only stop exits, the tier-2 path list.

---

## 2. `var/` layout — machine-local, gitignored, 0700

Everything here is produced by `ops.lib.paths` helpers; nothing builds these paths by hand.

```
var/state/mode.json              signed per-sleeve mode      ops.lib.mode_state
                                 read-only tri-state view    ops.lib.mode_view
var/state/config.bless.json      signed config digest        ops.lib.config_guard
var/runtime/freqtrade-<s>.mode.json   freqtrade overlay, 2nd --config   ops.gen_freqtrade_config
var/runtime/runtime-<s>.json          what the strategy reads ($EARN_RUNTIME)
var/runtime/compose.override.yml      exchange credentials, env references only
ops/locks/ops.lock               the one operations lock     ops.lib.oplock
logs/                            cron job logs
```

`ops.lib.paths` API (stdlib only, imports nothing from the repo):

```python
REPO_ROOT                      # the checkout this code was imported from
state_root(env=None)           # $EARN_STATE_ROOT or REPO_ROOT
data_path(rel, env=None)       # resolve a paths.* entry against the state root
var_dir() state_dir() runtime_dir() logs_dir() locks_dir()
mode_state_path() bless_path() ops_lock_path()
mode_overlay_path(sleeve) sleeve_runtime_path(sleeve) compose_override_path()
ft_run_db(run_id, sleeve)      # 'sqlite:////freqtrade/user_data/runs/<run_id>.sqlite'
ensure_dir(path, mode=0o700)   ensure_var_layout()   write_private(path, text)
in_worktree()  is_automated_run()
SLEEVES = ("a", "b")
```

### `var/state/mode.json`

```json
{"version": 1,
 "sleeves": {"a": {"state": "TEST", "submode": null, "run_id": "test-a-...", "seed_usdt": 10000},
             "b": {"state": "LIVE_PROPOSE", "submode": "propose", "run_id": "live-b-...", "seed_usdt": 500}},
 "set_at": "...Z", "set_by": "human:console:<sid>", "transition_id": 12,
 "nonce": "...", "sig": "hmac-sha256:..."}
```

States: `TEST`, `ARMING`, `DEMO_PROPOSE`, `DEMO_EXECUTE`, `LIVE_PROPOSE`, `LIVE_EXECUTE`,
`DISARMING`. `KILL` is an orthogonal overlay, not a state.

Three sets, and the distinction between them is load-bearing:

| set | members | what keys on it |
|---|---|---|
| `LIVE_MODES` | `LIVE_PROPOSE`, `LIVE_EXECUTE` | **real money** — `any_live`, the healthcheck's KILL branch, `autonomy.live_forces_human`, `_validate_strategies`. It must never grow. |
| `DEMO_MODES` | `DEMO_PROPOSE`, `DEMO_EXECUTE` | Binance Spot Demo Mode: real orders on `demo-api.binance.com`, fake money. Not TEST (nothing is simulated) and not LIVE (nothing is real). |
| `VENUE_MODES` | the union | "reaches an exchange": needs a credential, a venue binding, `dry_run: false` and a reconcile. |

`ops.lib.mode_state` API:

```python
load(path=None, *, secret=None, env=None) -> ModeState    # NEVER raises
build(sleeves, *, set_by, transition_id=None) -> ModeState
write(state, *, secret=None, path=None)                   # refuses under EARN_AUTOMATED_RUN=1
describe(state) -> str
ModeState.sleeve(s) -> SleeveState
ModeState.is_live(s) .is_demo(s) .needs_exchange(s)
ModeState.any_live() .any_demo() .live_sleeves() .demo_sleeves()
ModeState.verified: bool   .reason: str   .phase: 'paper'|'live_propose'|'live_execute'
SleeveState.state .submode .run_id .seed_usdt
SleeveState.is_live .is_demo .is_test .needs_exchange .executes .requires_approval
```

`phase` stays three-valued and a demo sleeve is `paper`: everything that reads `phase` is
asking how much **real capital** is at stake, and on demo the answer is none. Demo is
surfaced through `any_demo()` and the state word, never by widening `phase` or `is_live`.

**Fail closed.** Missing file, unreadable file, bad JSON, unknown shape, wrong version,
bad signature or absent secret ⇒ every sleeve is `TEST`, `verified=False`, and `reason` is
one of `missing | unreadable | bad_json | bad_shape | bad_signature | no_secret`. There is
no automated transition function: `ops.modes.transition()` (P5) needs a `HumanActor` that
only `console/deps.py` can build, and the only downward escape hatch is
`python -m console.cli set-mode --sleeve a --test`.

### `var/runtime/runtime-<s>.json`

```json
{"version": 1, "sleeve": "b", "mode": "live", "state": "LIVE_PROPOSE", "submode": "propose",
 "run_id": "live-b-20270201-01", "seed_usdt": 500.0, "require_approval": true,
 "approval_dir": "/freqtrade/proposals/approved", "generated_at": "...Z", "config_sha": "<sha256>"}
```

`GateConfig.load()` (P2) merges the committed `riskgate.json` with this file, and the
risk-state store namespaces every key with `run:<run_id>:`.

With **no** runtime file, `GateConfig.load()` falls back to
`strategies.riskgate.default_run_id(s)` = `test-<s>-000` — deliberately the same string
`ops.gen_freqtrade_config.default_run_id(s)` renders, so a gate running from the committed
baseline writes the rows a host-side reader expects. The two implementations are pinned
against each other by test; `strategies/` may not import `ops`, so the mirror is manual.
Rows written under the older `""` namespace still exist, which is why
`mode_view.risk_state` reads the bare key as a fallback — nothing creates that shape now.

### `ops.lib.mode_view` — liveness for jobs that hold no key

`EARN_CONSOLE_SECRET` is in **no** `envwrap.sh` allowlist, so in every unattended job
`mode_state.load()` returns `verified=False, reason='no_secret'`, which by the fail-closed
rule above reads back as *every sleeve TEST*. That is correct for a **writer** — nothing
unsigned may put a sleeve live — and wrong for a **reader that then acts on it**, because
"I could not read the authority" is not "the authority says TEST".

No job may treat unverified state as proof of TEST. `ops.lib.mode_view` is the **one**
helper they use instead, and it answers a tri-state:

```python
load(*, jdb=None, state=None, root=None, runtime_dir=None, env=None, sleeves=None,
     config_path=None) -> ModeView
ModeView.sleeve(s) -> SleeveView   .liveness(s)  .is_live(s) .is_demo(s) .is_test(s) .unknown(s)
ModeView.any_live()          # some sleeve is PROVABLY live (real money; demo never counts)
ModeView.any_demo()          # some sleeve is PROVABLY on Binance Spot Demo Mode
ModeView.any_assume_live()   # some sleeve is not provably TEST  <- restrictive predicate
SleeveView.liveness in ('live','demo','test','unknown')
SleeveView.is_live .is_demo .is_test .unknown .needs_exchange
SleeveView.assume_live .requires_approval .label() .describe()
SleeveView.corroborated      # provable ENOUGH to act destructively on — see §1.2
SleeveView.state .submode .run_id .seed_usdt .source .reason .state_reason .overlay
```

`demo` is a value of its own, not a flavour of either neighbour, and both halves matter.
It is **not TEST**, so no permissive "it's only paper" branch fires for a sleeve that is
placing real orders — in particular `ops.healthcheck` would otherwise have read a correctly
armed demo sleeve as "provably TEST reporting `dry_run=false`" and engaged KILL on it every
five minutes. It is **not LIVE**, so no real-money guard, report, NAV row or TCA
calibration counts it. `assume_live` is `!= TEST`, so every restrictive branch in the table
below already covers demo without being told about it.

`requires_approval` fails closed **in this order**: `UNKNOWN` ⇒ `True` first, before
anything else (an unprovable sleeve that merely kept a state word is not an approval-free
one, and no `require_approval` flag can buy it off, because that flag is on the file we
could not prove); then the overlay's own `require_approval`, which is literally what the
container enforces; then a *proven live* sleeve ⇒ `True` unless it is provably
`LIVE_EXECUTE` — liveness can be proven by the `dry_run` overlay alone, which carries no
state word, and `LIVE_PROPOSE` is a live submode; only a proven TEST answers `False`.

Evidence, in precedence order. Only (1) is authority; (2) and (3) are corroboration:

1. **`var/state/mode.json`**, when the signature verifies — the console, the CLI and tests
   still get the signed authority unchanged, and it outranks everything below.
2. **`var/runtime/runtime-<s>.json` + `var/runtime/freqtrade-<s>.mode.json`** — rendered by
   the *human's* transition from verified state (step 8), 0600 inside 0700 `var/`, already
   the strategy's and the container's own source of truth. `mode`/`state` must agree with
   `dry_run`, or the answer is UNKNOWN.
3. **`sleeve_runs` (the active run) + `mode_transitions` (the last `status='completed'`
   row)** — the journal's append-only record of what a human did.

(2) and (3) must agree with each other. Conflicting evidence, no evidence, or a transient
`ARMING`/`DISARMING` state all give `UNKNOWN`. `UNKNOWN` is never TEST.

**Every caller takes the restrictive branch on UNKNOWN, and restrictive means "assume real
money", not "assume TEST":**

| caller | on UNKNOWN |
|---|---|
| `ops/healthcheck.py:check_mode_consistency` | alert critical; **never** engage KILL. KILL needs a proof — a provably-TEST sleeve reporting `dry_run=false`, or a bot provably trading real money on `Scaffold` |
| `runs/reconcile_job.py:run` | reconcile against the **exchange**, with that sleeve's preflight baseline |
| `runs/apply_changes.py:effective_mode` | the **`live`** autonomy column, so `live_forces_human` engages |
| `runs/research_run.py:proposal_destination` | `approval_status='pending'` — a human must sign |
| `runs/nav_tick.py:run` | label the row `live` (`nav_points.mode` admits only `test\|live`); the sleeve is listed in the summary's `mode_unproven` |
| `ops/telegram_bot.py:cmd_mode` | print `unknown` and the reason |
| `ops/preflight.py` `kill_clear` | a lock it cannot rule out is a lock, **and** any venue-bound target (demo *or* live) fails the check outright: `refusing to arm from an unprovable state`. Disarming (target `TEST`) is exempt — the way back must never be blocked by not knowing |

### 1.2 The overlay's trust boundary — what `var/runtime` may license

The two `var/runtime` overlays are **unsigned**, and signing them would buy nothing: the
reader that needs them is precisely the reader with no `EARN_CONSOLE_SECRET`, so it could
never verify a signature it was handed. Making them authority would also be circular — the
container was *started from* that overlay, so "the overlay says TEST" and "the bot should
be dry-run" are the same claim, not two. The decision is therefore:

**The overlay is evidence with a provenance check, and it may never alone license a
destructive action.**

`mode_view` checks `runtime-<s>.json`'s provenance and reports it as `SleeveView.overlay`:

| `overlay` | when | effect |
|---|---|---|
| `current` | known `version`, `config_sha` equals the sha256 of `config/earn.yaml` on disk, and `generated_at` is **not older** than the newest completed `mode_transitions.started_utc` for the sleeve | may corroborate |
| `stale` | `generated_at` predates that transition — the file the container was started from, which the human has since superseded | **discarded outright**; the journal alone speaks, `reason='stale_overlay'` when nothing else does |
| `unverified` | no `generated_at`, unknown `version`, missing or mismatched `config_sha`, or only `freqtrade-<s>.mode.json` survives | still read for liveness, never corroborates |
| `absent` | nothing rendered | — |

A `runtime-<s>.json` naming a **different** `sleeve` is refused as evidence
(`reason='runtime_conflict'`).

`SleeveView.corroborated` is true only for the signed authority, or for two independent
sources that agree with a `current` overlay (`source='runtime+journal'`). Callers:

* `ops/healthcheck.py:check_mode_consistency` engages KILL for an unauthorised live bot
  only when the sleeve is `is_test` **and** `corroborated`. Provably-TEST from one
  unsigned file is an alert, not a flatten: a stale or hand-edited overlay would otherwise
  be enough to have an authorised live sleeve's book closed by a cron job. The other KILL
  case — a bot reporting `dry_run=false` while running `Scaffold` — rests on the bot's own
  report rather than on any overlay, so it is unaffected.
* Everything else already takes the restrictive branch on anything but a proven TEST, so
  `corroborated` changes nothing for them.

The same module owns the **run-scoped `risk_state` read**, because the namespace
`run:<run_id>:` is derived from the very overlay above:

```python
risk_state(jdb, sleeve, keys, *, run_id) -> {key: value}   # run-scoped row wins, bare is fallback
risk_flag(jdb, sleeve, key, *, run_id) -> bool             # EITHER row set (locks fail closed)
active_run_id(jdb, sleeve, *, view=None) -> str            # the overlay's, else the active run
```

Two blocking checks were silently inert without it: `ops/preflight.py`'s `kill_clear`
monthly-lock test and `runs/router.py`'s `near_stop` escalation flag.

### `ops.lib.signing`

```python
SECRET_ENV = "EARN_CONSOLE_SECRET"   APPROVAL_SECRET_ENV = "EARN_APPROVAL_KEY"
canonical(payload) -> bytes          # json, sort_keys, separators=(',',':'), 'sig' removed
sign(payload, secret) -> "hmac-sha256:<hex>"
verify(payload, sig, secret) -> bool         # constant time; no secret or no sig ⇒ False
sign_payload(payload, secret) -> dict        verify_payload(payload, secret) -> bool
get_secret(env_var=SECRET_ENV) -> str|None   require_secret(...) -> str   # raises SigningError
new_secret() new_nonce() sha256_file(path) sha256_text(text)
```

The canonical form is stdlib-only on purpose: the in-container proposal loader verifies
approvals with nothing but `json`, `hmac` and `hashlib`.

### `ops.lib.config_guard`

```python
BLESSED_FILES = ("config/earn.yaml", "config/models.yaml",
                 "config/freqtrade-a.json", "config/freqtrade-b.json", "config/riskgate.json")
digest(root=None) -> {rel: sha256}
bless(actor, *, reason=None, root=None, secret=None) -> payload
verify(*, root=None, secret=None) -> BlessResult(ok, reason, changed, missing, blessed_at, blessed_by)
changed_files(*, root=None) -> [rel]
```

Reasons: `ok | missing | bad_json | bad_version | bad_signature | no_secret | drift`.
Preflight check 3 is `config_guard.verify().ok`.

### `ops.lib.oplock`

```python
with oplock.acquire("mode.transition", timeout_s=30):   ...   # raises OpsLockBusy
oplock.is_held(path=None) -> bool     oplock.holder(path=None) -> str
```

`flock(2)` on `ops/locks/ops.lock`, so `flock(1)` from a shell script contends for the same
lock. Serialises: mode transitions, test-run resets, config apply/regen/restart, crontab
install, `apply_changes` merges. **The kill switch never takes it.**

Two locks coexist and an operator debugging a stuck job needs to know which is which:
`ops.lib.oplock` is the single repo-wide operations lock above, and it **waits** (with a
timeout). `ops.lib.locks` is per-job and **non-blocking** — it is what a cron line's
`flock -n ops/locks/cron-<job>.lock` and `runs.signals`' own job lock use, and a second
copy of the same job simply exits rather than queueing.

### `ops.lib.kill`

```python
kill_path(cfg, root=None)        # root defaults to paths.state_root(), NOT the checkout
is_engaged(cfg, root=None)   engage(cfg, reason, root=None)   reason(cfg, root=None)
stop_entries(bot) -> str                      # 'stopentry', falling back to 'stopbuy'
cancel_open_entry_orders(bot) -> [str]
enforce(cfg, *, bot_factory, flatten=False)   -> [{sleeve, ok, detail}]
engage_and_enforce(cfg, reason, *, root=None, bot_factory, flatten=False) -> (path, rows)
```

The file is written **first**, then the bots are told, so a crash between the two still
leaves the switch on. `console/routers/kill.py` and `ops/healthcheck.py` both call these —
there is no second implementation of "stop entries" anywhere.

### `ops.lib.audit`

```python
record(conn, *, actor, action, target=None, detail=None, result='ok', request_id=None) -> id
try_record(conn, ...) -> id|None        # never raises; for paths that must not fail
record_config(conn, *, actor, file, after_sha, changed_paths, diff, ...) -> id
mark_config_applied(conn, id)  recent(conn, limit=50)  config_history(conn, file)
actor_console(sid) actor_cli() actor_telegram() actor_system(job)
```

Actor strings: `human:console:<sid>` | `human:cli` | `human:telegram` | `system:<job>`.
Results: `ok` | `denied` | `failed` — a refusal is a row too.

---

## 3. Configuration

### `ops.config`

`load_config()` keeps its signature and its whole accessor surface; `config_version: 2`.

```python
load_config(path=None, *, root=None) -> EarnConfig      # raises ConfigError
config_schema() -> dict                                  # what the console builds forms from
max_weight_for(cfg, asset) -> float
seed_for(cfg, sleeve, *, state=None) -> float            # THE single reader of a sleeve's seed
slots_for(cfg) -> ["08:30", "16:00"]                     # THE single source of research slots
stage_prompt(cfg, stage) -> str                          # flags|brief|brief_short|classify|scan|validate
startup_candles(cfg) -> int                              # resolves trading.startup_candles: auto
trading_for(cfg, sleeve) -> TradingDefaults              # defaults deep-merged with the override
stoploss_on_exchange(cfg, sleeve, *, live) -> bool       # resolves on_exchange: auto
EarnConfig.phase -> 'paper'|'live_propose'|'live_execute' # COMPUTED from mode state
```

Removed as stored keys, still accepted with a `ConfigWarning`:

| v1 key | v2 replacement |
|---|---|
| `phase` | computed from `var/state/mode.json` |
| `sleeves.*.capital_usdt` | `modes.test.seed_usdt` / the live seed in the mode file; read through `seed_for()` |
| `triggers.{cooldown_hours,max_per_day,news_events,move_4h_pct,funding_abs_8h}` | `signals.planner.*` and `signals.scanner.detectors.*`; the legacy attributes are filled at load so `runs/triggers.py` keeps working |

**Schema annotations.** Every field carries `description` plus `x-tier`
(`human｜tier1｜generated｜invariant`) and `x-group`, and where they apply `x-unit`
(`fraction｜pct｜usdt｜minutes｜hours｜days｜bps`), `x-widget`
(`slider｜cron｜time｜duration｜path｜model-ref｜skill-ref｜pair｜secret-ref`), `x-effects`
(`regen｜restart:freqtrade-a｜restart:freqtrade-b｜crontab｜restart:telegram｜restart:console｜reset_required`),
`x-protected` and `x-help-md`. `tests/test_foundation/test_config_schema.py` walks the
schema and fails on any property missing the first three — so a new key reaches the UI with
no frontend change, or the build goes red. Use the `ops.config.F(...)` helper; it makes the
three mandatory arguments impossible to forget.

**`x-effects` is inherited, and the schema is its only owner.** It is declared on the
*section* field (`risk`, `trading`, `universe`, `exchange` carry
`ops.config.REGEN_AND_RESTART`; `sleeves`, `proposal`, `execution`, `sleeve_a`, `sleeve_b`,
`bounds`, `modes.live` carry `["regen"]`; `console`, `telegram`, `ops.schedules`,
`ops.cron_mailto`, `research.slots`, `modes.test.seed_usdt` carry their own), and both
flatteners resolve a leaf's effects by walking up to the nearest annotated ancestor. A leaf
that declares its own `x-effects` wins. `ops.config_store` used to keep a private
`SECTION_EFFECTS` table for this, which `console.schema_meta` could not see — so the two
disagreed about what a save implied. That table is gone.

Cross-validations (each with a test): stop ≤ `risk.stoploss_per_trade`; trailing distance
≥ 0.005; `1 + dca.max_adds + pyramid.max_adds ≤ risk.max_entries_per_trade`; market entries
need `risk.market_entries_allowed`; stage deadlines fit the run deadline, which fits the
cron timeout; slots are `HH:MM`, unique and sorted; live seed ceiling ≥ `4 ×
min_notional_usdt`; test seed > 0; `gray_zone[0] < min_score ≤ gray_zone[1]`; scoring
weights sum to 1; skill bindings exist on disk; `news.asset_keywords` covers the universe
and `news.event_keywords` covers the detector events; strategy names resolve to a class in
`strategies/` that subclasses `EarnBaseStrategy` (AST scan — no freqtrade import), refused
for a live sleeve and warned in test.

### `ops.models_config`

```python
load_models_cfg(path=None, *, overlay=OVERLAY_PATH) -> ModelsConfig
models_schema() -> dict
apply_overlay(base, overlay) -> ModelsConfig           # raises ConfigError outside its remit
ModelsConfig.task(name) .model_ref(alias) .tier_of(alias) .caps_for(alias)
```

The loader accepts **both** shapes. A v1 file (`tasks.<t>.model/escalation/fallback`,
`models: alias -> "pinned string"`) is converted into v2 chains; a fallback that is not a
declared alias becomes `on_all_failed`. **The committed `config/models.yaml` is v2 chains**
throughout — `models:`, `tasks:`, `auth:`, `providers:`, `capabilities:`, `switching:`,
`budget:` and `shadow:`. `runs/router.py` keeps a v1 *view* of it through a compatibility
shim, so its existing callers are unchanged; the v1 acceptance in the loader is for old
files on disk, not for this one.

`router.resolve(task)` returns the head of the chain **that the direct SDK path can
serve**, skipping local (Ollama) entries. Routing to a local model is `runs/llm/chain.run_task`'s
job, because only it can build a context pack; handing `runs/ingest.py` the string
`llama3.1:8b` for `decision_core.run_stage` would simply break it.

Tier-1 overlay `config/models-auto.yaml` may do exactly three things: **add** a
`models.<alias>` declaration (`{provider, id, tier}`; it may never redefine one
`models.yaml` owns), move `tasks.<t>.chain[0]` to a declared model of the same or higher
tier, and own the `shadow` block. Anything else is rejected at load with the offending
paths named. The addition is what `runs/maintenance.start_shadow` needs — it discovers a
model the human file has never heard of and must name it before `shadow.model` can point
at it — and it is inert until a chain or `shadow` references it, both of which are policed.
`runs/review_run` promotes a clean shadow window by writing `tasks.decide.chain`
(head replaced, tail restated), **not** `tasks.decide.model`, which the whitelist refuses.

---

## 4. Database — `SCHEMA_VERSION = 3`

`ops.db` API:

```python
SCHEMA_VERSION = 3   SQL_DIR   MIGRATIONS   SCRIPT_MIGRATIONS
connect(path, *, readonly=False)     # writers: WAL, busy_timeout 5000, synchronous NORMAL
                                     # readers: mode=ro URI, busy_timeout 2000
opened(path, *, readonly=False)      # context manager that actually CLOSES the connection
write(conn, sql, params, *, retries=3)   # BEGIN IMMEDIATE + jittered backoff on 'locked'
apply_schema(conn, ddl_path, target_version=SCHEMA_VERSION)
init_all(cfg, root=None) -> (journal_path, knowledge_path)
journal_path(cfg, root=None)  knowledge_path(cfg, root=None)   # honour EARN_STATE_ROOT
has_column(conn, table, column)   utc_now()
```

Console reads use `opened(path, readonly=True)`, one per request, closed in a `finally`.

**New journal tables** (spec §4.1): `audit_log`, `config_audit`, `mode_transitions`,
`sleeve_runs`, `nav_points`, `signals`, `signal_validations`, `llm_calls`,
`provider_switches`, `provider_health`, `proposal_approvals`, `change_events`,
`reconciliations`, `backtest_runs`, `console_jobs`.

**`nav_points.positions_json` / `nav_daily.positions_json` hold base-unit AMOUNTS**, keyed
by base asset — `{"BTC": 0.04, "ETH": 0.5}`, coins and not money. Both writers
(`runs/nav_tick.py:ledger_nav`, `runs/nav_job.py`) agree on that shape. A consumer that
needs a weight or a gross number must mark the amounts itself (candle closes, via
`console.services.overview_service.marks`) or derive the total from money the ledger
already reconciled (`nav_usdt - cash_usdt` is the marked value of the book by
construction). An amount that cannot be marked is reported as unknown, never as zero.

**Additive columns** (`MIGRATIONS[3]`): `runs.provider`, `runs.chain_index`,
`runs.switched_from`, `runs.signal_id`; `proposals.signal_id`, `proposals.approval_status`;
`orders.mode`, `orders.run_id`; `fills.mode`, `fills.run_id`; `nav_daily.run_id`;
`incidents.subkind`; `ops_runs.detached_pid`, `ops_runs.rerun_started_utc`.

**Rebuilds** (`ops/sql/migrations/003_journal.sql`, SQLite's 12-step procedure between
`PRAGMA foreign_keys=OFF` and `PRAGMA foreign_key_check`, committed only if that check is
clean, row ids preserved):

* `gate_decisions.callback` now also accepts `adjust_trade_position`, `custom_exit`,
  `custom_stoploss`, `reconcile`; gains `run_id`, `action`, `trade_id`.
* `change_log` gains `op` (default `edit`), `author_run_id`, `branch`, `worktree`,
  `source_commit`, `claimed_evidence_json`, `verified_evidence_json`, `checks_json`,
  `revert_of`, `reverted_by`, and the statuses `verifying`, `reverted`, `superseded`.

Every new table is in `journal.sql` too, and the rebuild also runs once on a fresh database,
so every table v3 **creates or rebuilds** carries identical DDL in a fresh and a migrated
database (asserted by `test_migrated_schema_equals_fresh_schema`). Tables that only gained
`MIGRATIONS[3]` columns are **not** identical: SQLite's `ALTER TABLE … ADD COLUMN` leaves
the original `CREATE` text in `sqlite_master` and appends the column at the end, so
`runs`, `orders`, `fills`, `nav_daily`, `incidents` and `ops_runs` have a different column
*order* on a migrated database. Never index a row by position — every connection sets
`row_factory = sqlite3.Row`, so read columns by name.

Documented `ops_state` keys: `ollama_base_url`, `ollama_probe_at`,
`claude_auth_degraded_until`, `gate_breach_cursor`, `reconcile_cursor`, `install_utc`,
`rate_limit_status`, `rate_limit_utilization`, `rate_limit_resets_at`.

---

## 5. Generated configs

`ops.gen_freqtrade_config`:

```python
build_bot_config(cfg, sleeve)          -> dict    # committed, ALWAYS dry_run: true
build_riskgate_json(cfg, config_path)  -> dict    # committed, phase = COMMITTED_PHASE = 'paper'
build_params(cfg, sleeve)              -> dict    # seeded once, then owned by apply_changes
build_mode_overlay(cfg, sleeve, state) -> dict    # var/runtime/freqtrade-<s>.mode.json
build_sleeve_runtime(cfg, sleeve, state, config_path) -> dict   # var/runtime/runtime-<s>.json
build_compose_override(cfg, state, *, env=None) -> str  # var/runtime/compose.override.yml
venue_of(sleeve_state)                 -> Venue | None   # the mode's venue; None = no exchange
render_runtime(cfg, *, state=None, runtime_dir=None, env=None) -> {path: content}
write_runtime(cfg, *, state=None, runtime_dir=None, env=None) -> [path]
main(argv)     # --check (drift, committed files only), --no-runtime
```

Rules other packages depend on:

* the committed `config/freqtrade-*.json` always says `dry_run: true`, so no config save can
  flip a bot live;
* an overlay that reaches an **exchange** — live *or* demo — renders only from a verified
  mode state; anything else renders TEST;
* the venue is bound to the mode in code (`ops.lib.exchange_endpoints.MODE_VENUE`), never in
  `earn.yaml`. `DEMO_*` renders `dry_run: false` plus
  `exchange: {demo_trading: true, _ft_has_params: {supports_demo_trading: true}}` — both
  halves always together, because freqtrade ships `binance.supports_demo_trading = False`
  and refuses the key on its own. `LIVE_*` renders `dry_run: false` and no exchange block;
* no generated or committed compose layer may spell an exchange URL. `exchange.urls` and
  `FREQTRADE__EXCHANGE__URLS__*` are read by nothing in freqtrade 2026.8, so such a line is
  a silent no-op that reads like routing; `ops.lib.compose.audit_committed_layers` and
  `assert_no_exchange_url_override` refuse one;
* `riskgate.json` carries `risk`, `universe`, `proposal`, `execution`, `sleeve_b`, plus
  `trading` (timeframe, resolved `startup_candles`, `plan_bounds`, per-sleeve merged
  mechanics) and `bounds`, stamped with `source_sha256` of `earn.yaml` and with `profile`,
  the name of the active overlay (`""` for the shipped configuration) — `source_sha256`
  cannot answer that, because a profile does not change `earn.yaml`;
* a **profile** is a named overlay over `earn.yaml`, selected by `profiles.active` and read
  from `profiles.dir`. It is deep-merged at load time and the result is re-validated by the
  whole schema and every cross-check, so it can never say something `earn.yaml` could not.
  `ops.config.PROFILE_ALLOWED_PREFIXES` is an allowlist — `trading`, `execution`,
  `sleeve_a`, `sleeve_b`, `sleeves.<s>.strategy` — and everything else is refused by name;
  `assert_profile_preserves_protection` then re-derives both configurations and refuses the
  profile unless `risk`, `bounds`, `universe`, `modes`, `autonomy` and `exchange` are
  identical and no per-trade stop got looser. `assert_profile_not_live` refuses to render a
  runtime for real money while any profile is active: TEST and DEMO only. A profile reaches
  the bots only through the blessed generated files, so preflight sees it;
* no exchange key or secret is ever written into any generated file — the compose override
  carries env references only, and the NAMES are chosen from the sleeve's venue:
  `${BINANCE_KEY_<S>}` / `${BINANCE_SECRET_<S>}` for live, `${BINANCE_DEMO_KEY}` /
  `${BINANCE_DEMO_SECRET}` for demo, `""` for TEST. The two name sets are disjoint and a
  service block that mentions another venue's name is a render-time refusal, so a demo
  container cannot be handed a live key by a one-word `.env` edit. Each service also carries
  `EARN_VENUE`, so `docker inspect` answers "which Binance is this?" without reading JSON.

### 5.1 The universe snapshot the gate enforces (package U2)

`build_bot_config` and `build_riskgate_json` both take an optional `snapshot=` (the
resolver's `knowledge/universe/<date>.json`; omitted means `latest_snapshot()`), so the
**bot whitelist and the gate's tier table come from one artefact**. `max_open_trades` is
`risk.max_open_positions`, never `len(pairs)`, and `pairlists` is always `StaticPairList`.

`gate_universe_block(snap)` reduces the resolver's full snapshot to the five maps the
deterministic gate needs, keyed by **base asset**, and that is what lands in
`riskgate.json` under `universe.snapshot`:

```jsonc
"universe": {
  "pairs": ["BTC/USDT", "..."],          // the tradeable tier, incl. exit_only names
  "snapshot": {
    "date": "2026-09-23", "sha256": "…", // the identity a proposal cites (U3, v4)
    "tiers":   {"BTC": "core", "SOL": "major", "TIA": "satellite", "X": "watchlist"},
    "caps":    {"SOL": 0.15},            // the resolver's cap; may only TIGHTEN the ceiling
    "scores":  {"TIA": 0.87},            // satellite score, higher is better (§3.2)
    "exit_only": ["OLD"],                // resolver flag OR a delisting_at in the snapshot
    "filters": {"TIA": {"min_notional": 5.0, "step_notional": 0.03}}
  }
}
```

What the gate does with it (`strategies/riskgate.py`, `UniverseView`):

* **cap resolution, tightest wins** — `exit_only` ⇒ 0; else the tightest of
  `risk.max_weight[asset]`, `risk.tier_caps[tier]` and the snapshot's `caps[asset]`;
  **no tier and no explicit entry ⇒ 0.0**, and the `tier:<asset>` check refuses the order.
  There is no `risk.max_weight.default` any more, and nothing may reintroduce one.
* **new refusal reasons**, in `CHECK_ORDER`: `exit_only:<asset>`, `tier:<asset>`,
  `step_size:<pair>`, `min_position`, `max_positions`, `satellite_count`,
  `satellite_gross`, `beta_cap`, `corr_cap`.
* **beta and correlation** are computed by the gate from a `returns_provider(pair)`
  injected by the adapter (`EarnBaseStrategy._daily_returns`, 60 daily observations from
  the 1d informative frame). Unmeasurable — no history, one name, a constant series —
  passes; the tier cap and `max_satellite_gross` bound that window.
* an absent or unparseable `snapshot` block degrades to the two-asset world: every asset
  with an explicit `risk.max_weight` entry is `core` and nothing else is tradeable.

Callers of `ops.config.max_weight_for(cfg, asset, tier=None)` must pass the tier when they
have one; without it only an explicit `max_weight` entry yields a non-zero cap.

### 5.2 The snapshot file itself (package U1)

`knowledge/universe/<YYYY-MM-DD>.json`, written by `ops.universe_refresh` and **committed**:
it is simultaneously the whitelist the bots run and the whitelist the backtests replay.
`ops/universe.py` owns the format and is the only reader anything else should use.

```python
# ops.universe — pure except fetch_inputs()
resolve(symbols, bars, now, *, rules, tiers, score,
        delistings, listed_at, retain) -> Snapshot
fetch_inputs(*, quote, history_days, max_workers) -> FetchResult   # Binance public, no key
load_current(dir) -> Snapshot | None        # newest file; cached on (name, mtime, size)
load_snapshot(path) / write_snapshot(snap, dir) / snapshot_paths(dir) / latest_snapshot_path(dir)
diff(old, new) -> Diff                      # added / removed / tier_changes / exit_only
Snapshot.tradeable_pairs / .watchlist_pairs / .tradeable_assets / .watchlist_assets
Snapshot.tier_of(asset) / .cap_of(asset) / .satellites(limit) / .sha256
ops.config.resolver_args(cfg) -> (Rules, Tiers, ScoreRules)   # membership from universe.*,
                                                              # caps from risk.*
```

```jsonc
{ "schema_version": 1, "date": "2026-09-23", "generated_at": "…Z", "quote": "USDT",
  "rules": {…every threshold…}, "tiers": {…}, "score_rules": {…},
  "counts": {"watchlist": 107, "tradeable": 31, …}, "funnel": [{"step":1,"filter":…}],
  "pairs":    { "BTC/USDT": { "symbol","base","tier","cap","score","rank",
                              "tick_size","step_size","min_notional",
                              "delisting_at","exit_only","exit_reason","exit_only_since",
                              "metrics": {…} } },
  "excluded": { "BTTCUSDT": "tick_size:tick=270.0bps" },   // why every name is OUT
  "sha256": "…" }
```

Rules other packages depend on:

* `sha256` is over the canonical payload with the `sha256` key removed. A proposal cites
  `{date, sha256}` (`ops.config.universe.snapshot_ref`) so it stays replayable after the
  universe moves. `load_snapshot` **refuses** a file whose digest does not match.
* `universe.assets` / `universe.pairs` / `universe.watchlist_pairs` /
  `universe.watchlist_assets` on `EarnConfig` are **computed** from the newest snapshot,
  core first; with no snapshot they fall back to `universe.core`. They are not YAML keys
  any more (a legacy `assets:`/`pairs:` pair warns and maps `assets` → `core`).
* **tradeable tier** = `core | major | satellite`; **watchlist** adds `watchlist`.
  `data_only_symbols` are excluded from every tier by the resolver, so they can never be
  traded, and are added back only by `ops.universe_refresh.pair_list(cfg, "download")`,
  which is what the candle scripts use.
* an `exit_only` pair stays on the whitelist with `cap: 0.0`; the refresh also raises a
  per-pair `universe_exit_only:<pair>` flag (`severity block_entries`, `scope <pair>`)
  through `ops.lib.flags`, which `flags.entries_blocked(path, pair)` already honours.
* **falling out of the universe is never a disappearance.** Every pair the previous
  snapshot could trade is offered back to the resolver (`retain`); one that no longer
  qualifies returns at its old tier as `exit_only`, cap 0, with the filter it now fails in
  `exit_reason` and the date in `exit_only_since`. It drops for good after
  `universe.refresh.exit_only_weeks` (4). That is what makes a depeg, a halt or a slow
  slide below the satellite floor a wind-down instead of an orphaned position — and it is
  why the shrink guard counts pairs that can still be **entered**, not the whitelist.
* `metrics.listing_age_days` is exact: the fetcher asks for each symbol's first ever candle
  (`startTime=0, limit=1`) as well as the recent page, because `/klines` caps a page at
  1,000. `metrics.age_is_lower_bound` is true only when that answer was unavailable, and
  `ops.check_gaps` refuses to relax its history floor on an age that is only a bound.
* delisting notices are **input**, in `knowledge/universe/delistings.json`
  (`{"<SYMBOL or PAIR>": "<ISO 8601>"}`), written by `reg-watch` or a key-holding job.
  Binance's schedule is a `sapi` endpoint needing an API key, which `ops/envwrap.sh` gives
  to `reconcile` and `preflight` only — and demo has no `sapi` tier at all, so freqtrade's
  `DelistFilter` is deliberately **not** wired in.
* `ops/refresh_backtest_data.sh` runs the refresh and then tops up candles, in that order;
  `universe.refresh.cron` must equal `ops.schedules.backtest_data.cron` (cross-validated).

---

## 6. `runs/llm` — the LLM contract

`runs/llm/types.py` is stdlib-only and imports nothing at runtime (`StageMeta`/`StageResult`
come from `runs.decision_core` under `TYPE_CHECKING`), so any process can depend on it.

```python
ModelRef(alias, provider, model_id, tier)          .is_local
ProviderCaps(structured_output, tools_readonly, tools_write, skills, max_ctx)   .satisfies(req)
LLMRequest(task, prompt, model, output_schema, tools_profile, allowed_tools, skills,
           cwd, env, max_turns, max_usd, effort, deadline_s)                    .with_deadline(s)
Attempt(idx, ref, status, error, latency_ms, cost_usd, input_tokens, output_tokens, auth_source)
RunCtx(run_id, stage, kind, deadline_at, signal_id, journal_path, cwd, extra)   .remaining_s(now)
TaskResult(ok, text, meta, attempts, switched, served, failure, fallback_action)
Provider (Protocol): key, caps, health() -> bool, run(req) -> StageResult
LLMError, AuthError, BudgetExhausted, ProviderDown, SchemaInvalid, CapabilityError
required_caps(tools_profile, *, output_schema=False, skills=False) -> ProviderCaps
chain_for(task, refs, *, min_tier=1, allow_local=True) -> [ModelRef]
classify_is_terminal(failure) -> bool
```

Vocabularies:

* `FAILURE_CLASSES` — `error, timeout, rate_limited, auth_error, quota_exhausted,
  budget_exhausted, provider_down, schema_invalid, empty_output, skipped_open_circuit,
  skipped_capability`. These are the keys of `models.yaml: switching.on` and the reasons in
  `provider_switches`.
* `CALL_STATUSES` — exactly the `llm_calls.status` CHECK: `ok` plus every failure class
  except `provider_down` (a skipped provider produces a `provider_switches` row, not an
  attempt row).
* `SWITCH_ACTIONS` — `next, skip, retry_then_next, stop`.
* `PROVIDER_KEYS` — `claude:subscription, claude:api_key, ollama`; also the
  `provider_health.provider_key` values.
* `TERMINAL_FAILURES` — `budget_exhausted, quota_exhausted`: never retried.
* `MIN_TIER_FLOOR = {"decide": 4, "validate": 3}` and
  `ALWAYS_LOCAL_FORBIDDEN = {"decide"}` — **code, not config**. No config edit and no
  tier-1 overlay can let a local model write a proposal or a validation; `chain_for()`
  enforces it for every consumer, including the fakes.

`runs/llm/base.py`: `BaseProvider` (capability gating via `can_serve`/`ensure_can_serve`),
`ProviderRegistry` + the process-wide `registry`, and `classify_error(exc)` /
`classify_text(text)` — the one place a provider error becomes a failure class.

`runs/llm/stub.py`: `StubProvider` + `scripted(...)`, the shared fake. It answers from a
queue, records every `LLMRequest`, raises the scripted `LLMError` (or returns a failing
`StageResult` with `raises=False`), and honours capability gating.

`runs/llm/chain.py`, `runs/llm/providers/{claude_sdk,ollama}.py`, `runs/llm/health.py` and
`runs/llm/context_packs.py` have all landed. `chain.run_task(task, prompt, …) -> TaskResult`
is the entry point: it walks the task's chain, applies `switching.on`, honours the circuit
breakers and the budgets, and journals every attempt in `llm_calls` and every switch in
`provider_switches`. `research_run`, `ingest`, `review_run` and `daily_review` still reach
the model through `router.resolve()` + `decision_core.run_stage` for their own stages;
that works through the shim but gets no chain fallback and no `llm_calls` rows, and
migrating each one is its owning package's call.

### `runs.signals` — the pipeline other packages read

```python
runs.signals.pipeline.{scan, plan, funnel, mark_acted, record_manual}
runs.signals.outcomes.{resolve_due, stats}
runs.signals.features.{build, watchlist_pairs, core_pairs, held_pairs, rank_watchlist,
                       CHEAP_KEYS}
runs.signals.detectors.{run_all, max_per_detector, Ctx.pairs, Ctx.priority}
schemas.signals.{validate_screen, validate_validation}
schemas.proposal.{build_models, validate_proposal, parse_any, to_file,
                  tradeable_from_snapshot, universe_ref_from_snapshot}
```

`schemas.proposal` is the ONE place the proposal shape is defined, and
`strategies/proposal_loader.py` (stdlib-only, in-container) re-checks it structurally.
The two must agree: `OPTIONAL_FIELDS` there covers `plan`, `signal_id`, `schema_version`
and `universe_snapshot`; `ASSET_PATTERN` and `DEFAULT_MAX_ASSETS` are byte-equal on both
sides (a test asserts it); and `PLAN_FIELDS` covers `schemas.proposal.Plan`'s
`entry_style, stop_pct, take_profit_pct, dca_allowed, valid_for_hours` (plus `urgency`,
which Earn never writes and `clamp_plan` drops unless `trading.plan_bounds.allow_model_urgency`).

#### Proposal `schema_version: 4` — sparse targets over a dynamic universe

```jsonc
{ "schema_version": 4,
  "targets": { "BTC": 0.40, "SOL": 0.05, "USDT": 0.55 },   // SPARSE: absent == zero
  "universe_snapshot": { "date": "2026-09-23", "sha256": "…64 hex…" },
  … every v3 field unchanged … }
```

* `targets` keys match `^[A-Z0-9]{2,12}$`. The quote (`USDT`) is **always required** —
  cash is never implicit. Every other key must be in the **tradeable tier** of the cited
  snapshot; a key outside it rejects the **whole** proposal rather than being clamped, so
  the journal records a model that tried. At most `risk.max_open_positions` non-quote keys.
* `universe_snapshot` is required exactly when `schema_version >= 4`, and forbidden below
  it. `runs/research_run.py` passes the snapshot it resolved its tradeable set from
  (`cfg.universe.snapshot_ref`) into `validate_proposal(..., snapshot=…)`, so an answer
  citing a different snapshot is refused as `universe_snapshot … is not the snapshot this
  run used`.
* `to_file` stamps the LOWEST version a payload needs: 4 with a snapshot, 3 with a `plan`
  or `signal_id`, nothing otherwise. `parse_any` still replays v2 and v3 files, including
  ones holding an asset that has since left the tradeable tier.
* `schemas/proposal.json` is the rendered artefact (SDK `output_format`) and is generic:
  JSON Schema pins the shape and the cap, membership of the tradeable tier is pydantic's
  job because it depends on a snapshot JSON Schema cannot see.

#### Scanner breadth and the token budget

The scanner reads its universe from the features, not from config: `features.build`
computes `CHEAP_KEYS` (6 keys, daily candles + one batched news query) for the whole
`universe.watchlist_pairs`, and the full 24-key set only for core + held + the highest
ranked names (`signals.scanner.rich_pairs`, default 20). `Features.rich` says which.
Detectors iterate `Ctx.pairs`; a rich-only key on a cheap pair reads `None` and the
detector does not fire. `run_all` caps each detector at
`signals.scanner.max_candidates_per_detector` (default 3) ordered by `Ctx.priority`
(fast path → core → tradeable → strength) and reports the drops in `Ctx.capped` /
`ScanReport.capped` — a cap is not an error. `pipeline.scan` spends
`max_candidates_per_cycle` in that same priority order.

Measured on the real 2026-09-23 snapshot (107 watchlist pairs): features block 8.4k
tokens, whole scan prompt 15.3k against a 20k budget; research prompt 2.5k with an
840-token UNIVERSE block naming all 31 tradeable assets, their tiers and their caps.

### Approval files — `proposals/approved/<run_id>.json`

Written by `runs/approvals.py` (console or Telegram), read by the in-container loader:

```json
{"version": 1, "run_id": "2026-09-22T08:30+04:00", "decision": "approved",
 "decided_utc": "...Z", "expires_at": "...Z", "expires_utc": "...Z",
 "actor": "human:console:<sid>", "channel": "console",
 "proposal_sha256": "<sha256 of the proposal file>", "note": null,
 "nonce": "...", "sig": "hmac-sha256:..."}
```

The vocabulary differs from the `proposal_approvals` row on purpose: the row's CHECK is
`approve`/`reject` with `expires_utc`, the file says `approved`/`rejected` with
`expires_at`, and it carries both expiry keys so each side reads the same signed bytes.
`proposal_loader.approval_for(dir, run_id, now, proposal_path=…)` verifies the HMAC, the
decision, the expiry **and** `proposal_sha256` against the file it is about to act on — so
an approval cannot be replayed against a rewritten proposal.

---

## 7. Schedules and jobs

`config/earn.yaml: ops.schedules` is the single source for cron expressions, `timeout(1)`
budgets and the healthcheck artifact name. `research.slots` is the single source for research
fire times (`slots_for(cfg)`), the `nearest_slot` computation and the `proposals_file`
artifact name.

v3 jobs: `scanner` (`*/5`), `nav_tick` (`*/15`), `reconcile` (`*/15`). Deadlines raised so
each run plus its postflight fits inside the cron timeout: `research_run` 2700 s,
`review_run` 6600 s, `daily_review` 3300 s.

`ops/crontab` is **generated** by `ops/gen_ops_files.py` and must never be hand-edited:
`tests/test_ops/test_gen_ops_files.py::test_committed_crontab_is_the_template` asserts the
committed file equals `render_crontab(cfg, template_ctx())`. It renders one line per
`research.slots` entry (the 16:30-vs-16:00 fix), the quoted root, the venv on `PATH`, the
`mkdir -p` guard and `MAILTO` from `ops.cron_mailto`. `gen.JOBS` maps each
`ops.schedules` key to the module or script it runs; `reconcile` runs `runs.reconcile_job`
(not `runs.reconcile`, which does not exist). `ops/envwrap.sh` carries the matching
`scanner` / `nav_tick` / `reconcile` / `signals` allowlists.

**Claude auth in `envwrap.sh`** — `EARN_CLAUDE_AUTH_MODE` from `.env`, unknown ⇒
`subscription`:

| mode | what the job gets |
|---|---|
| `subscription` | `CLAUDE_CODE_OAUTH_TOKEN` only under that name; a present API key is **renamed** to `EARN_FALLBACK_ANTHROPIC_API_KEY`, never exported as `ANTHROPIC_API_KEY` (which would preempt subscription auth in a headless run) |
| `api_key` | `ANTHROPIC_API_KEY` only; the OAuth token is dropped. The one mode that exports the plain name |
| `auto` | the token (if any) **plus** the key renamed to `EARN_FALLBACK_ANTHROPIC_API_KEY` |

The rename is **unconditional in both `subscription` and `auto`** — a host with no
subscription token still gets the protected name, because neither mode may let the CLI
pick a key up implicitly: metered spend is only ever a deliberate, journalled, capped
`claude:api_key` attempt. The key is not *discarded*, only made un-preemptable; `api_key`
mode is the only one that exports the plain name. Nothing is lost:
`claude_auth.api_key_from()` reads the protected name first, `env_for('claude:api_key')`
materialises it under `ANTHROPIC_API_KEY` for one explicit attempt, and
`claude_auth.resolve_any()` (which `require()` and the ingest classifier use) counts it as
a credential. `claude_auth.resolve()` stays a faithful mirror of the CLI's own chain and
deliberately does not see it.

---

## 8. Invariants shown read-only in the UI

| Invariant | Enforced by |
|---|---|
| Console binds 127.0.0.1 only | `console/settings.py` (code Literal) |
| `EFFORT_FLOOR = "high"` | `runs/router.py:clamp_effort` |
| `ALWAYS_DISALLOWED` tools | `runs/decision_core.py` |
| Stop exits are market orders | `strategies/earn_base.py` |
| `decide` needs tier ≥ 4, `validate` tier ≥ 3 | `runs/llm/types.py:MIN_TIER_FLOOR` |
| No local model writes a proposal | `runs/llm/types.py:chain_for` |
| Committed bot configs are always `dry_run` | `ops/gen_freqtrade_config.py:build_bot_config` |
| Unverified mode state ⇒ TEST | `ops/lib/mode_state.py:load` |
| No job treats unverified state as proof of TEST | `ops/lib/mode_view.py:load` (tri-state) |
| Mode writes refuse under `EARN_AUTOMATED_RUN=1` | `ops/lib/mode_state.py:write` |
| The kill switch never waits for the ops lock | `ops/lib/kill.py:engage_and_enforce` |
| Tier-2 paths | `.claude/hooks/tier2_paths.py` |

---

## 9. The console — FastAPI surface (F0b), and what every package plugs into

### 9.1 The `/api` prefix and the router convention

**A feature router declares a bare area prefix. `console/app.py` adds `/api` on mount.**

```python
# console/routers/<area>.py — one area, one file, one owner
router = APIRouter(prefix="/mode", tags=["mode"])      # served at /api/mode/...
```

A module whose routes span several top-level paths (`decisions.py` serves `/runs` and
`/proposals`) exports `APIRouter(tags=["decisions"])` with no prefix and spells the full
path on each route. Earlier drafts of this file and of spec §13 said
`APIRouter(prefix="/api/<area>")`; that was wrong and three packages had to be corrected
for it. `register_router` mounts a router that already spells `/api` as-is rather than at
`/api/api/...`, so the mistake is survivable, but `tests/test_console/test_api_skeleton.py`
asserts against it.

Seams in `console.app`:

```python
discover_routers(package=console.routers) -> [(name, router)]   # pkgutil, sorted by name
register_router(app, router, prefix="/api")                     # the only way to add routes
iter_routes(app) -> [(methods, full path)]                      # walks INCLUDED routers too
startup_recovery(app) -> dict                                   # see 9.5
shutdown(app)
```

Discovery imports every module in `console.routers` at start-up, so an import error in one
area fails the console loudly rather than silently dropping routes.

### 9.2 Dependencies — `console.deps`

`Annotated` aliases, which are what a route signature should use (a bare
`= Depends(...)` default is what ruff's B008 flags):

```python
Actor       = Annotated[HumanActor, Depends(current_actor)]     # 'S' in the spec's table
StepUpActor = Annotated[HumanActor, Depends(require_step_up)]   # 'SU'
Cfg         = Annotated[EarnConfig, Depends(cfg_dep)]
Settings    = Annotated[ConsoleSettings, Depends(get_settings)]
Jdb         = Annotated[sqlite3.Connection, Depends(get_jdb)]   # mode=ro, closed in finally
Kdb         = Annotated[sqlite3.Connection, Depends(get_kdb)]
```

plus `get_cfg`, `config_sha`, `journal_db`, `knowledge_db`, `audit_event`, `bot_api`,
`bot_factory`, `http_error`, and `stop_entries` / `cancel_open_entry_orders`, which are
**re-exports of `ops.lib.kill`** — the console and `ops/healthcheck.py` (which engages KILL
unattended) run one implementation, not two.

Every route carries `Actor` or `StepUpActor`. The only exceptions, and the only ones
allowed, are `GET /api/health`, `POST /api/auth/login` and `GET /api/openapi.json`.
`tests/test_console/test_route_inventory.py` asserts exactly that, and asserts the `SU`
set against spec §5.2.

### 9.3 `app.state`

| attribute | what |
|---|---|
| `settings` | frozen `ConsoleSettings`; `BIND_HOST` is a code `Literal["127.0.0.1"]` |
| `bus` | `console.sse.EventBus` |
| `signer` | `console.security.SessionSigner` (stdlib HMAC; **not** itsdangerous) |
| `rate_limiter` | `console.security.RateLimiter` |
| `jobs` | `console.services.jobs.JobRunner` |
| `bot_factory` | `None` in production; a test sets it to inject a fake bot |
| `backtest_queue` | created lazily by `console/routers/backtests.py` |
| `startup` | the recovery report from 9.5 (inspectable; deliberately not in `GET /api/meta`, whose fields are mirrored by hand in the frontend under an exact-set drift test) |

### 9.4 Errors, auth and transport

One envelope, everywhere, from middleware and handlers alike:

```json
{"error": {"code": "...", "message": "...", "detail": null}}
```

| status | code | when |
|---|---|---|
| 401 | `unauthorized` | no session |
| 403 | `forbidden` / `bad_origin` / `csrf_failed` / `step_up_required` | |
| 404 | `not_found` | |
| 409 | `conflict` / `sha_conflict` | optimistic-concurrency etag mismatch (`/api/config` emits `sha_conflict`, from `ops/config_store.py`) |
| 415 | `invalid` | a mutating request that is not `application/json` |
| 421 | `bad_host` | `Host` is not loopback with our port |
| 422 | `invalid` / `invalid_config` | request body failed validation (`/api/config` emits `invalid_config` with the per-path issues in `detail`) |
| 423 | `locked` | ops lock held |
| 429 | `rate_limited` | |
| 503 | `automated_run` / `unavailable` | `EARN_AUTOMATED_RUN=1`; or config/DB missing |

Session cookie `earn_session` (HttpOnly, SameSite=Strict), CSRF header `X-Earn-CSRF`
double-submitted against the session. Token file `$XDG_CONFIG_HOME/earn/console-token`
(0600, `$EARN_CONSOLE_TOKEN_FILE` overrides); the salted hash lives in
`var/state/console_auth.json`. **No response ever carries a secret value.** Three checks
enforce it: a static field-name scan over every DTO the app *serves*
(`console.contracts.RESPONSE_MODELS` plus `mounted_response_models(app)`, so a package that
declares its DTOs in its own router module is covered too), a live crawl of every GET route
and the SSE stream against planted sentinels, and `console.security.redact_body` scrubbing
every JSON/markdown/plain body on the way out. A secret-shaped fact is reported as
`present` / `last4` / a path.

SSE at `GET /api/stream`, hand-written (**not** sse-starlette), `{topic, id, ts, payload}`
frames, 15 s heartbeat, `?topics=`/`?last_event_id=`/`?limit=` and the `Last-Event-ID`
header. Topics: `alert health kill mode transition bot nav order fill gate signal
validation run proposal approval provider_switch config change job reconcile backtest`
plus the parametric `log:<name>`.

`log:<name>` is published by `console.events.LogFollower`, one per file being watched.
`GET /api/logs/{name}` serves the tail **and** arms a follower from the end of what it
served (`?follow=false` opts out); the response names the `topic`. New lines are redacted
by the same `ops_service.redact` the tail uses, capped at `LOG_MAX_BYTES_PER_POLL` per read
and `LOG_MAX_LINES_PER_EVENT` per event (the payload reports what it `dropped`), and a
shorter file restarts the offset with `truncated: true`. A follower with no subscriber on
its topic for `LOG_IDLE_GRACE_S` stops itself. Clients reach it with `useTopicEvents`: the
browser shell adds a parametric topic to its one connection rather than opening a second.

Reads go through `console.services.queries` (`mode=ro`, `busy_timeout=2000`, 3 retries);
`QueryError.reason` is `missing | locked | error`. Background work goes through
`console.services.jobs.JobRunner` / `JobSpec` (`needs_lock=True` takes the ops lock), which
writes a `console_jobs` row and a redacted traceback file. A job also reports output:
`progress.log(line)` publishes batched `{"event": "output", "id", "lines"}` frames on the
`job` topic (status frames carry `"event": "status"`), and every run keeps the last
`OUTPUT_LINES` of it. **Every run reaches a terminal status** — the worker finishes it from
a `finally` even when the thread dies, `shutdown()` closes out what would not stop, and
`jobs.mark_interrupted` reconciles rows whose process died. A verdict is final: nothing
re-opens a finished run.

### 9.5 Start-up and shut-down

`console.app.startup_recovery` runs once in the lifespan, before the first request, and
nothing in it may stop the console from booting:

1. `mode_service.recover_on_start(cfg, root=paths.state_root(), bot_factory=…)` — a sleeve
   left in `ARMING`/`DISARMING` by a crashed transition is stopped from entering and
   forced back to `TEST`.
2. `backtest_service.mark_interrupted(conn)` and `jobs.mark_interrupted(conn)` — both the
   queue and the job runner are in-process, so a `queued`/`running` row that survived a
   restart is a lie an operator would wait on. They are failed (`killed` for a job).
3. An ops-lock probe: `flock(2)` dies with its holder, so a lock still held at start-up
   means a live process owns it. Reported, never acted on.

Shut-down stops the backtest queue and calls `JobRunner.shutdown()`, which cancels
in-flight runs cooperatively, joins their threads, and marks `cancelled` anything that did
not stop in time — so no row and no card is left claiming to run.

### 9.6 The mounted route table

This is the surface, and it is asserted: `tests/test_console/test_route_inventory.py`
parses the block below and fails if `iter_routes(create_app())` differs in either
direction. Adding an endpoint means adding a line here.

```routes
GET    /api/approvals/history
GET    /api/approvals/pending
GET    /api/audit
GET    /api/audit/entry/{source}/{ref}
GET    /api/audit/sources
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
POST   /api/auth/rotate-token
POST   /api/auth/step-up
GET    /api/autonomy
PUT    /api/autonomy
GET    /api/backtests
POST   /api/backtests
GET    /api/backtests/{bt_id}
POST   /api/backtests/{bt_id}/cancel
GET    /api/bots
GET    /api/bots/{sleeve}
POST   /api/bots/{sleeve}/forceexit
DELETE /api/bots/{sleeve}/locks/{lock_id}
DELETE /api/bots/{sleeve}/orders/{trade_id}
POST   /api/bots/{sleeve}/restart
POST   /api/bots/{sleeve}/start
POST   /api/bots/{sleeve}/stopentry
GET    /api/changes
GET    /api/changes/timeline
GET    /api/changes/{change_id}
POST   /api/changes/{change_id}/approve
POST   /api/changes/{change_id}/attach
POST   /api/changes/{change_id}/reject
POST   /api/changes/{change_id}/revert
GET    /api/config
GET    /api/config/drift
GET    /api/config/effects
POST   /api/config/effects/apply
GET    /api/config/{file_id}
PUT    /api/config/{file_id}
POST   /api/config/{file_id}/defaults
GET    /api/config/{file_id}/history
POST   /api/config/{file_id}/preview
POST   /api/config/{file_id}/revert
GET    /api/control
GET    /api/control/acting
POST   /api/control/flatten
GET    /api/control/liveness
PUT    /api/control/level
POST   /api/control/pause
GET    /api/control/schedule
POST   /api/control/schedule
GET    /api/control/spend
POST   /api/control/start
POST   /api/control/stop
GET    /api/control/supervisor
POST   /api/control/units
GET    /api/health
GET    /api/invariants
GET    /api/invariants/strip
GET    /api/invariants/{invariant_id}
POST   /api/jobs/research/run
DELETE /api/kill
GET    /api/kill
POST   /api/kill
GET    /api/knowledge/briefs
GET    /api/knowledge/briefs/{date}
GET    /api/knowledge/dossiers
GET    /api/knowledge/dossiers/{asset}
GET    /api/knowledge/grades
GET    /api/knowledge/incidents
GET    /api/knowledge/news
GET    /api/knowledge/sources
GET    /api/knowledge/state
DELETE /api/llm/claude/signin
GET    /api/llm/claude/signin
POST   /api/llm/claude/signin
POST   /api/llm/claude/signin/code
GET    /api/llm/local-model
GET    /api/llm/ollama/detect
GET    /api/llm/ollama/models
POST   /api/llm/ollama/pull
POST   /api/llm/playground
GET    /api/llm/providers
POST   /api/llm/providers/{key:path}/circuit/reset
POST   /api/llm/providers/{key:path}/test
GET    /api/llm/routing
GET    /api/llm/switches
GET    /api/llm/usage
GET    /api/logs
GET    /api/logs/{name}
GET    /api/market/candles
GET    /api/market/markers
GET    /api/market/pairs
GET    /api/meta
GET    /api/meta/schema
GET    /api/mode
POST   /api/mode/preflight
POST   /api/mode/recover
GET    /api/mode/runs
POST   /api/mode/transition
GET    /api/mode/transitions
POST   /api/mode/transitions/{transition_id}/rollback
GET    /api/ops/backups
POST   /api/ops/backups/run
POST   /api/ops/backups/test-dest
GET    /api/ops/containers
POST   /api/ops/containers/{service}/restart
GET    /api/ops/db
GET    /api/ops/health
GET    /api/ops/host
GET    /api/ops/incidents
POST   /api/ops/incidents/{incident_id}/close
GET    /api/ops/jobs
POST   /api/ops/jobs/{job}/run
GET    /api/ops/jobs/{job}/runs
GET    /api/ops/schedules/crontab
POST   /api/ops/schedules/install
GET    /api/ops/systemd
GET    /api/overview
GET    /api/overview/demo
GET    /api/overview/nav
GET    /api/perf/attribution
GET    /api/perf/nav
GET    /api/perf/runs/{run_id}
GET    /api/perf/summary
GET    /api/perf/whatif
GET    /api/portfolio/{sleeve}
GET    /api/portfolio/{sleeve}/fills
GET    /api/portfolio/{sleeve}/nav
GET    /api/portfolio/{sleeve}/orders
POST   /api/portfolio/{sleeve}/orders/{trade_id}/cancel
GET    /api/preview
POST   /api/preview
GET    /api/prompts
PUT    /api/prompts/active
GET    /api/prompts/diff
POST   /api/prompts/{family}/versions
GET    /api/prompts/{path:path}
PUT    /api/prompts/{path:path}
POST   /api/prompts/{path:path}/render
GET    /api/proposals
GET    /api/proposals/pending
GET    /api/proposals/{run_id:path}
POST   /api/proposals/{run_id}/approve
POST   /api/proposals/{run_id}/reject
GET    /api/reports
GET    /api/reports/{rel:path}
GET    /api/risk
GET    /api/risk/flags
GET    /api/risk/gate-decisions
GET    /api/risk/{sleeve}/anchors
GET    /api/risk/{sleeve}/limits
GET    /api/risk/{sleeve}/mechanics
POST   /api/risk/{sleeve}/resume-monthly
GET    /api/risk/{sleeve}/utilisation
GET    /api/runs
GET    /api/runs/{run_id:path}
GET    /api/runs/{run_id:path}/trace
GET    /api/search
GET    /api/search/index
GET    /api/secrets
PUT    /api/secrets/auth-mode
POST   /api/secrets/test/{target}
DELETE /api/secrets/{name}
PUT    /api/secrets/{name}
GET    /api/signals
GET    /api/signals/funnel
POST   /api/signals/manual
POST   /api/signals/scan-now
GET    /api/signals/{signal_id}
POST   /api/signals/{signal_id}/label
POST   /api/signals/{signal_id}/revalidate
GET    /api/skills
POST   /api/skills
GET    /api/skills/jobs/{job_id}
POST   /api/skills/jobs/{job_id}/cancel
POST   /api/skills/{name}/archive
PUT    /api/skills/{name}/bindings
POST   /api/skills/{name}/eval
GET    /api/skills/{name}/files/{path:path}
PUT    /api/skills/{name}/files/{path:path}
POST   /api/skills/{name}/lint
PUT    /api/skills/{name}/policy
POST   /api/skills/{name}/test
GET    /api/skills/{name}/tree
POST   /api/skills/{name}/trial
GET    /api/stream
GET    /api/testruns
GET    /api/testruns/compare
GET    /api/testruns/summary/{sleeve}
GET    /api/testruns/{run_id}
POST   /api/testruns/{sleeve}/reset
```
