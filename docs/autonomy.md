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
* A `skill_new` change that touches any `scripts/**` is **always** held for a human, no
  matter what the matrix says.
* The tier-2 path list itself.

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
7. `skill_new` + `scripts/**` ⇒ always held.
8. `autonomy.tier1_auto_merge`, then `autonomy.kinds[kind][mode]`.
9. The weekly auto-merge cap.

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
| `skill.*` | `evals/skill_lint.py`, the skill's own pytest, and `evals/skill_eval.py` at or above `skills.eval.min_pass_rate` |

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
`tca` and `risk-gate` keep human-only scripts and tests.

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
