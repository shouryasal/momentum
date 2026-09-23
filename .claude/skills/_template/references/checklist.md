# New-skill checklist

Everything here is checked by `evals/skill_lint.py` or by the skill's own tests. A change
that creates a skill is verified against these before it can merge, so failing one is a
rejection, not a note.

## Frontmatter

- [ ] `name` equals the directory name, lowercase, hyphens only.
- [ ] `description` is at least 40 characters, third person, and names the trigger words.
- [ ] `allowed-tools` is a subset of:
      `Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**),
      Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)`.
- [ ] No `WebFetch`, no `WebSearch`, no wildcard `Bash`.
- [ ] The name shadows no existing skill and no tool name.

## Layout

```
.claude/skills/<name>/
  SKILL.md          the body (tier 1)
  references/*.md   the long-form material the body points at (tier 1)
  scripts/*.py      deterministic helpers (TIER 2 — human-written only)
  tests/test_*.py   required; run by the gate
  evals/cases.yaml  required; pass rate gates the change
```

## Tests first

Write `tests/test_*.py` before the body. A skill whose tests were written afterwards tests
what the body happens to say, not what the skill is for.

## Evals

`evals/cases.yaml` declares the cases `evals/skill_eval.py` runs:

```yaml
cases:
  - id: computes-the-table
    run: scripts/example.py
    args: ["--dry-run"]
    expect:
      exit_code: 0
      stdout_contains: ["rows="]
```

A case with `ask:` needs a model session; the code-only gate *skips* it and a skipped case
is not a pass, so declare at least enough `run:` cases to clear
`skills.eval.min_pass_rate`.

## Scripts

`scripts/**` is tier 2. An automated session may not write one, and a `skill_new` change
containing any is always held for the human. Prefer a skill that reasons over scripts that
already exist.

Banned in any script, enforced by AST scan: `subprocess`, `socket`, `httpx`, `requests`,
`urllib`, `os.system`, `eval`, `exec`, and writes to anything outside `knowledge/` and
`reports/`.
