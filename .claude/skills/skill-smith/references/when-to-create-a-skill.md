# When a new skill is the right fix

Earn's self-improvement loop can change four things: a parameter, a prompt, a skill, or a
model. Picking the wrong one is the most common way a review week produces motion without
improvement. This is the decision table.

| Symptom | Fix | Why |
|---|---|---|
| A limit is too tight or too loose and the backtest says so | `params` | Numbers are parameters; nothing has to be *taught* |
| The decision missed something that was in the inputs | `prompt` | The procedure exists, the instruction did not point at it |
| The decision did not know a procedure exists | `skill` | A missing procedure is exactly what a skill is |
| The same procedure is written out in three reports | `skill` | The repetition is the evidence |
| A better model would have caught it | `model` | And it needs a shadow window, not an opinion |
| The code computed the wrong number | none of these | That is a tier-2 bug: write it up for the human |

## The two-week rule

A root cause with `fix_path = skill` in **one** week is a hypothesis. The loop is allowed
to be wrong once, and a skill written from one bad week teaches the loop to overfit to that
week. Two distinct review weeks with the same `recurrence_key` is the threshold, and the
change must cite both event ids.

## The rule of three

A procedure repeated three times is cheaper to write down than to rediscover. Fewer than
three, and the write-up costs more context than it saves — every loaded skill spends the
prompt's token budget whether it fires or not (`budgets.context_tokens`).

## What "good" looks like afterwards

A skill earns its place if, four weeks later:

- the `recurrence_key` that justified it has stopped appearing, **or**
- the procedure it captured now runs without being restated in the report.

If neither is true, the honest move is a `skill` change with `op: delete`. A skill that
fires and changes nothing is worse than no skill: it costs context on every run and it
looks like the problem is handled.

## Why scripts are human-only

A skill's `scripts/**` is deterministic code that runs with the repository on `PYTHONPATH`.
Letting an automated session write one would put a model back in the order path by a side
door — it could compute the numbers the gate checks. So `scripts/**` is tier 2, the AST
denylist refuses network and filesystem escapes even from human-written scripts, and a
`skill_new` containing any script is held for the human regardless of the autonomy matrix.
