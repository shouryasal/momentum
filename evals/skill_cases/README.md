# Trusted skill eval fixtures

`evals/skill_cases/<skill-name>.yaml` holds the eval cases **the change gate** scores a
skill change against. This directory is tier 2 (`evals/**`), so no automated session can
write anything in it — which is the entire point.

A skill also ships its own `.claude/skills/<name>/evals/cases.yaml`. That file is tier 1:
the same commit that rewrites the skill may rewrite its cases. It is what the skill author
and the console's **Eval** button use, and the gate ignores it completely
(`evals.skill_eval.load_cases(..., trusted_root=...)`).

**A skill with no file here cannot be scored, so a change to it is held for you.** That is
deliberate: a pass rate computed from a fixture the candidate's own commit could have
written is not evidence.

## Format

The same shape as a skill's own cases file:

```yaml
cases:
  - id: btc-drawdown-table
    run: scripts/asset_stats.py        # relative to the skill folder, and it must stay inside it
    args: ["--asset", "BTC", "--dry-run"]
    expect:
      exit_code: 0
      stdout_contains: ["max_drawdown"]
      stdout_not_contains: ["Traceback"]

  - id: names-the-invalidation
    ask: "Grade run 2026-09-20T08:30+04:00 and name the invalidation."
    expect:
      contains: ["invalidation"]
```

`run:` cases execute a script. `ask:` cases need a session runner; without one they are
*skipped*, and a skipped case is not a free pass — it still counts in the denominator.

## Before you add one

Script cases run model-authored code. They execute only through the sandbox described in
`docs/autonomy.md`: with `security.agent_user` and `security.agent_cli_wrapper` unset,
`evals.skill_eval` refuses to run them at all and the change is held. Writing fixtures
here does not on its own make a skill change mergeable — configuring the sandbox is the
other half.
