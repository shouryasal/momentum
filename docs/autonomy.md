# Autonomy — what the system may change about itself

Earn improves itself. That is the point of it. This page is the answer to the only
question that matters afterwards: **what exactly was it allowed to touch, and how do I
take it back?**

Short version: a Claude run may freely write its own knowledge and notes; it may propose a
narrow set of edits to its own parameters, prompts and skills, which are merged only after
**code recomputes the evidence**; and it can never touch anything that decides how orders
are placed, how risk is enforced, or whether money is real.

---

## The three tiers

The one source of the boundary is `.claude/hooks/tier2_paths.py`. The PreToolUse hook
enforces it inside automated runs (`EARN_AUTOMATED_RUN=1`), and `runs/apply_changes.py`
refuses to merge any commit touching a tier-2 path — so an automated session cannot get
past it either by writing a file or by committing one.

### Tier 0 — free, every run

`knowledge/**`, `reports/**`, `changes/**`, `proposals/**`, `lessons.md`,
`lessons-archive.md`.

Its own observations, briefs, dossiers, grades, reports and change proposals. Written
directly, no gate. Two carve-outs are pulled *back* into tier 2 because code owns them:
`knowledge/flags.json` (written only through `ops.lib.flags`) and `knowledge/state/**`
(the computed market state).

### Tier 1 — gated, through `changes/*.json`

| Path | What |
|---|---|
| `config/params-sleeve-{a,b}.json` | Strategy parameters, each clamped to `bounds:` in `earn.yaml` |
| `config/models-auto.yaml` | Model overlay: may **add** a model alias, move `tasks.<t>.chain[0]` to a declared model of the same or higher tier, and own the `shadow` block. Nothing else, refused at load |
| `prompts/**` | Prompt bodies, including `prompts/stages/*.md` |
| `.claude/skills/<name>/` bodies and tests | Except `ops-runbook` (human end to end), and except `tca`/`risk-gate` tests |

Never merged directly: a session edits these **in a git worktree**, commits on its own
branch, and writes a `changes/<id>.json`. `runs/apply_changes.py` decides.

### Tier 2 — human only, always

`config/**` (except the two tier-1 carve-outs), `strategies/**`, `runs/**`, `evals/**`,
`ops/**`, `console/**`, `schemas/**`, `tests/**`, `journal/**`, `data/**`,
`ft_userdata/**`, `var/**`, `.env*`, `.claude/settings.json`, `.claude/hooks/**`, every
skill's `scripts/**`, `.claude/skills/ops-runbook/**`, `config/skills-registry.auto.yaml`,
`config/prompts-auto.yaml`, `.gitignore`, `pyproject.toml`, `.pre-commit-config.yaml` and
`CLAUDE.md`.

Reading is restricted too: during an automated run `.env*`, `var/state/**`,
`var/runtime/**`, `~/.config/earn/**` and `~/.claude/.credentials.json` cannot be read by
any tool, and Bash commands that reach the network or the loopback console are refused.

---

## What can never change, in any mode

These are code. There is no config key, no overlay and no autonomy setting that moves them
— the **Invariants** page shows each one with the `file:function` that enforces it.

* The console binds `127.0.0.1` only.
* `decide` needs a tier-≥4 model and may never be served locally; `validate` needs tier ≥ 3
  (`runs/llm/types.py: MIN_TIER_FLOOR`, `chain_for`). No 8B model on your laptop writes a
  proposal.
* The reasoning-effort floor is `high` (`runs/router.py: clamp_effort`).
* Stop exits are market orders (`strategies/earn_base.py`).
* The committed `config/freqtrade-*.json` always say `dry_run: true`, so no config save can
  flip a bot live.
* Unverified mode state means TEST, everywhere, always (`ops/lib/mode_state.py: load`).
* Mode writes refuse under `EARN_AUTOMATED_RUN=1`.
* The kill switch never waits for the ops lock.
* A `skill_new` **or `skill_edit`** change that touches any `scripts/**` is **always** held
  for a human, no matter what the matrix says.
* A part of a skill that `skills.policy` marks `human` is **always** held.
* Model-authored code is never executed uncontained — see the next section.
* The tier-2 path list itself.

---

## Executing model-authored code

Two files inside a skill are tier 1 — a review session may write them — and both are
*executed*: `tests/**` (by the change gate and by the console's **Test** button) and
`evals/cases.yaml`, whose `run:` field names a script to run. That is model-authored code,
so the tier boundary does not cover it and the PreToolUse hook cannot see it: the writes
happen inside a subprocess, not a tool call.

What contains it now:

| Layer | What holds it |
|---|---|
| **Written** | `evals/skill_lint.py` walks `tests/**/*.py` (the wider `profile="tests"` rule set) and checks every `run:` in `evals/cases.yaml`. Naming `.env`, `var/state`, `var/runtime`, `killdir`, `config/earn.yaml` or `.claude/hooks` is an error, in a script or a test. A `run:` that resolves outside the skill folder is an error. |
| **Executed** | Only through `evals/skill_eval.py`, which takes a **required** `Sandbox`. No sandbox means *refused*, and a refusal is a **hold** — never a pass. |
| **Environment** | An allowlist (`skill_eval.CONTAINED_ENV`): `PATH`, `HOME`, locale, temp dir. No credential can be inherited, including one added to `.env` tomorrow, and no `EARN_*` pointer is handed over either. Under the uid sandbox the wrapper rebuilds the environment itself (`envwrap.sh` → `env -i`), so what actually decides is the **job name** it is given: `skill_eval.EVAL_JOB`, whose envwrap allowlist is empty. A test asserts that. |
| **Time** | `skills.eval.timeout_s`, enforced by `subprocess.run(timeout=…)`. |
| **Evidence** | The gate scores a skill change against `evals/skill_cases/<name>.yaml` — tier 2, so no session can write it — and never against the skill's own `evals/cases.yaml`. A commit that also rewrites its own `tests/**` is held: the evidence and the thing being judged may not have the same author. |
| **Session** | Neither the weekly nor the daily review has a `Bash(pytest …)` rule any more, and the hook refuses every inline interpreter and heredoc (`python -c`, `sh -c`, `perl`, `pytest`, `<<EOF`) outright. |

### The strongest containment available, and the residual risk

The real boundary is a **separate uid**. Set both of these and every model-authored
subprocess runs through the wrapper as that user:

```yaml
security:
  agent_user: earn-agent          # created by ops/setup_agent_user.sh
  agent_cli_wrapper: ops/agent_cli.sh
```

Both ship as `null`. **In that state nothing model-authored is executed at all**:

* the change gate's `skill_tests` and `skill_eval` checks report
  `refused: security.agent_user / security.agent_cli_wrapper are not configured…` and the
  change is **held** for you — it cannot merge on evidence that was never gathered;
* the console's **Test** and **Eval** buttons report the same refusal instead of running.

That is the deliberate trade: a skill change cannot self-certify until you have configured
a uid to contain it. **Residual risk, stated plainly:**

* a uid drop is not a container. A contained test can still *read* anything world-readable
  on the host and can reach the network if the host allows it. It cannot read `.env`,
  `var/`, `ops/` or `.git/config` if `ops/setup_agent_user.sh` set the ownership it
  documents — verify that, because the wrapper is trusted here;
* that same setup deliberately gives the agent user **write** access to the tier-0/tier-1
  data dirs (`knowledge/`, `journal/`, `reports/`, `changes/`, `proposals/`, `lessons.md`,
  `prompts/`). A contained test can therefore still scribble in them. Nothing there moves
  money or code on its own — a `changes/*.json` still needs a commit the gate verifies and
  a merge only `apply_changes` can perform — but treat those directories as data an
  untrusted process can touch, not as evidence;
* the lint is an AST denylist, not a proof. It stops the demonstrated payloads (reading
  `.env`, deleting the kill switch, importing `socket`) and a computed path is a warning,
  not an error, so a determined author can still obscure a destination;
* with `agent_user` configured, a change gate that says PASS is asserting the *sandbox*
  ran the tests, not that the tests are honest. The self-authored-evidence hold and the
  tier-2 fixtures are what carry that half.

---

## The autonomy matrix

`config/earn.yaml: autonomy` (tier 2, step-up-protected, editable on the
**Self-Improvement** page):

```yaml
autonomy:
  tier1_auto_merge: true           # master switch; false = every tier-1 change is HELD
  kinds:                           # kind -> {test|live: auto|approve|off}
    params:     { test: auto,    live: approve }
    prompt:     { test: auto,    live: approve }
    skill_edit: { test: auto,    live: approve }
    skill_new:  { test: approve, live: approve }
    skill_bind: { test: approve, live: approve }
    model:      { test: approve, live: approve }
    revert:     { test: auto,    live: auto }
  live_forces_human: true
  max_auto_merges_per_week: 5
  auto_revert: { enabled: true, window_days: 7, validity_drop_pct: 10, breach_increase: 1 }
```

`test` and `live` are not sleeve modes but the **system's** mode: `live` the moment *any*
sleeve is `LIVE_*`, otherwise `test`. Unverified mode state reads as `test` because
everything is TEST then anyway.

`live_forces_human: true` means that in live mode nothing but `revert` may sit on `auto`.
`revert` is deliberately exempt — undoing a change that is hurting you must work precisely
when it matters most.

**So, in practice**, with the shipped defaults: while you are in TEST, the system may merge
its own parameter tweaks, prompt edits and skill-body edits on its own, at most 5 per week,
and may revert anything. New skills, skill bindings and model changes always wait for you.
The moment a sleeve goes live, everything except revert waits for you.

---

## How a change is made

```
review_run (Sun 20:00) / daily_review (21:30)
  git worktree add -B <kind>/<key> ../earn-worktrees/<kind>-<key> <git.live_branch>
  data dirs symlinked back to the live root; session runs with
    cwd = the worktree,  EARN_STATE_ROOT = the live root,  EARN_WORKTREE = the worktree,
    EARN_AUTOMATED_RUN = 1,  a narrowed Bash allowlist
  the session edits tier-1 files IN THE WORKTREE, commits on its branch,
    and writes changes/<id>.json (one commit per change, touching only its target)
apply_changes  (postflight, in the LIVE checkout, under the ops lock)
```

The live checkout never changes branch, never resets and never moves. `apply_changes` is
the only automated writer of the live HEAD.

### What `apply_changes` checks, in order

1. `changes/<id>.json` validates against `schemas/change.json`.
2. `tier == 1`.
3. The declared target is not tier-2.
4. **`evals/verify_change.py` recomputes the evidence.** This is the important one — see
   below.
5. The monthly parameter-change budget (`review.change_gates.max_param_changes_per_month`).
6. `tier1_freeze` is not active (it is raised when a TCA gap is unexplained).
7. `skill_new` or `skill_edit` + `scripts/**` ⇒ always held.
8. Any part of the skill that `skills.policy` marks `human` ⇒ always held.
9. `autonomy.tier1_auto_merge`, then `autonomy.kinds[kind][mode]`.
10. The weekly auto-merge cap.

Then `merge()` cherry-picks (`git cherry-pick -x`) the change's commit onto
`git.live_branch` in the live checkout, tags it `change/<id>`, and writes a `change_events`
row. **Every refusal is a hold, never a rejection** — a conflicted or unverifiable change
stays alive for the next run or for you.

### Why the model's own numbers never count

The failure mode this design exists for: a session that wants its change merged writes
flattering numbers into the file it authored. So the claimed numbers are stored in
`change_log.claimed_evidence_json` and shown next to the verified ones — and **only what
`evals/verify_change.py` recomputes can gate anything**:

| Kind | Recomputed |
|---|---|
| every kind | the commit touches nothing but the declared target; no tier-2 path; `author_model` equals what `runs.served_model` says actually served `author_run_id` |
| `params` | bounds and max step, recomputed from `git show <commit>:<target>` against the live file; a costed backtest over `change_gates.backtest_min_years`; walk-forward candidate vs baseline, out of sample |
| `prompt`, `skill` | `evals/replay.py` with **cwd = the candidate worktree** and the stage's bound skills allowed, so the two arms genuinely differ; the counterfactual is computed from the replay output; scanner/validator prompts additionally get `evals/signal_replay.py` precision/recall against resolved signal outcomes |
| `skill.*` | `evals/skill_lint.py` (including `tests/**` and `evals/cases.yaml`), the skill's own pytest **inside a sandbox**, and `evals/skill_eval.py` against the tier-2 fixtures in `evals/skill_cases/<name>.yaml` at or above `skills.eval.min_pass_rate`. No sandbox ⇒ refused ⇒ held. A commit that rewrites its own `tests/**` ⇒ held |

A claimed number more than 10 % from the verified one is flagged red in the UI and written
as a `root_cause_events` row with `recurrence_key = 'evidence_mismatch'`. **A runner that
is not available yields a hold** — an unverifiable change waits for a human; it never
merges by default.

### Change statuses

`proposed → verifying → auto_merged | approved | rejected | held | reverted | superseded`.

---

## How a skill gets created

New skills are rare on purpose: a skill is a permanent tax on every future run's context.
The `skill-smith` skill will only start when one of two things is true:

1. the same `recurrence_key` appears in `root_cause_events` in **two distinct review
   weeks** with `fix_path = skill` — the loop has already tried to fix it with a parameter
   and failed; or
2. the same procedure appears in **three** reports or sessions.

Then it copies `.claude/skills/_template/`, **writes the tests first**, writes the eval
cases in `.claude/skills/<name>/evals/cases.yaml`, writes the body, and checks it with the
template checker. One commit touching only `.claude/skills/<name>/`, written as a
`kind: skill, op: create` change.

A merged `skill_new` lands **incubating** in `config/skills-registry.auto.yaml` and is
loaded by nothing. Binding it to a task is a separate `skill_bind` change, which the
shipped matrix always routes to you. A new skill may contain no `scripts/**` at all —
deterministic code is yours to write.

`skills.policy` in `earn.yaml` records who may edit what per skill: the default is
`{body: gated, scripts: human, tests: gated}`; `ops-runbook` is `human` for all three;
`tca` and `risk-gate` keep human-only scripts and tests. **`apply_changes` reads it**
(`human_only_parts`) and holds any change whose commit touches a part marked `human`, so
the marking you see on the Skills page is the marking the gate enforces — it used to be a
label with no reader but the listing endpoint.

---

## Approving, rejecting and reverting

### In the console (the normal way)

**Self-Improvement** (`/self-improvement`) → the change queue. Open a change and you get
claimed vs verified evidence side by side with mismatches in red, the checks list, the git
diff and the event timeline. Then:

* **Approve** — merges it now (`POST /api/changes/{id}/approve`).
* **Reject** — closes it with your reason.
* **Revert** — `git revert --no-edit <merge_commit>` on the live branch.
* **Attach** — link the change to an incident or a root cause.

The same page holds the autonomy matrix editor and the auto-revert settings, plus the
merge/revert timeline and the recurring root causes.

### From a terminal

```bash
.venv/bin/python -m runs.apply_changes                        # run the gate over the queue
.venv/bin/python -m runs.apply_changes --approve <change_id> --note "why"
.venv/bin/python -m runs.apply_changes --reject  <change_id> --note "why"
.venv/bin/python -m runs.apply_changes --revert  <change_id> --note "why"
```

The actor is recorded as `human:cli`. With no flags it processes the whole queue and
prints `<id>: <status> (<reason>)` per change.

### Automatic revert

`daily_review` watches every change merged inside `autonomy.auto_revert.window_days`
(default 7). If the change's own metric crosses a threshold — validity dropping by
`validity_drop_pct` (10 %) or gate breaches rising by `breach_increase` (1) — it writes an
`auto_revert_requested` event **and stops**. It never reverts anything itself.
`apply_changes.run_auto_reverts()` performs the revert on the next pass, as
`system:auto_revert`, and only while `autonomy.auto_revert.enabled` is true.

This is why `revert: {test: auto, live: auto}` is the one row that stays on `auto` in live.

### Turning it all off

Set `autonomy.tier1_auto_merge: false` (Self-Improvement page, or Settings). Every tier-1
change then lands as **held** and waits for you. Nothing is lost — the branches and commits
survive, and approving later merges them normally.

---

## Where to look afterwards

| Question | Where |
|---|---|
| What did it change, and when? | `/self-improvement`, or `/audit` filtered to `change_events` |
| What evidence did it actually have? | The change drawer: verified column, not claimed |
| Which commit is it? | `change_log.merge_commit`, and the `change/<id>` git tag |
| Which model authored it? | `change_log.author_model`, cross-checked against `runs.served_model` |
| Which branch and worktree? | `change_log.branch` / `.worktree`; worktrees live under `git.worktree_root` and are pruned after `git.worktree_ttl_days` |
| Did a hook stop something? | `/invariants` → last hook denials |
