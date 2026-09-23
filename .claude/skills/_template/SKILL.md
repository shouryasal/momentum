---
name: {{SKILL_NAME}}
description: One sentence saying what this skill does for Earn and what it refuses to do, then "Triggers on <the words a run would actually use>". At least forty characters, third person, no "you" or "I".
allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# {{SKILL_TITLE}}

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2 — a human writes those, and a skill_new that contains any is
     always held for the human. -->

## When this runs

Name the trigger precisely. A skill that "helps with analysis" fires on everything and
therefore teaches nothing.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| … | `knowledge/…` | `python3 ${CLAUDE_SKILL_DIR}/scripts/….py` |

Numbers come from scripts. Never estimate a price, a return or a volatility from memory —
that is the standing rule in CLAUDE.md and it applies inside every skill.

## Procedure

1. …
2. …
3. …

## Hard stops

- What this skill must refuse to do (say it as a rule, not a preference).
- What it never writes: anything outside `knowledge/` and `reports/`.
- When it abstains instead of guessing.

## Output

Exactly what the caller gets back, in what shape, and where any file lands.
