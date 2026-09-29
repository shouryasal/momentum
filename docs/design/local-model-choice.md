# Which local model should serve Earn's cheap tier

Measured 2026-09-25 on the live host (Windows + WSL Ubuntu-24.04, Ollama at
`http://172.29.64.1:11434`, ~6 GB GPU). 209 local model calls and 32 Haiku calls over a
corpus built from the real runtime: 50 real scanner snapshots rendered through
`runs/signals/screener.render_prompt`, 88 real `news_items` rows rendered through the real
`classify` template, and the 8 real `watch_events` prompts reconstructed from their recorded
`facts_json`.

**Recommendation in one line:** set `local_small` to `granite4.2:3b`, keep `num_ctx` /
`max_ctx` at 8192, and fix three things that matter more than the model choice — the
provider must send `"think": false`, the scan prompt must shrink from ~20–30k tokens to
~4k, and `dip_from_high` must stop citing a feature key that does not exist.

Nothing in `config/models.yaml` has been changed by this work. The exact values to set are
in [§8](#8-exact-configuration-to-set).

---

## 1. The headline: no local model is screening anything today

`tasks.scan` is `chain: [local_small, haiku]` with `num_ctx: 8192`. The real scan prompt is
**19,253–30,704 tokens** by the models' own tokenizers. It does not fit, and it never has.

The router knows: `fits_context` skips the local model with `skipped_capability` on every
cycle, and has done since 2026-09-23. So the first question is not "which local model
screens best" but "what happens if one is ever handed this prompt" — because that is the
state anyone raising `num_ctx` would create.

I ran that deliberately, passing the provider an unlimited declared `max_ctx` so the guard
could not intervene. Batch 5, 5 calls per model, on both a loaded host (loadavg ~33) and a
quiet one (loadavg ~1), with identical outcomes:

| model | outcome when handed the real prompt at `num_ctx: 8192` | host-verified items kept |
|---|---|---|
| `granite4.2:3b` | HTTP 400 `exceed_context_size_error` in 0.08–6.3s, 5 of 5 → `error` | 0 / 0 |
| `qwen3.5:4b-q4_K_M` | **silently truncated to 4,098 prompt tokens**, answers confidently in 66–144s | **0 / 86** |
| `llama3.1:8b` | 2 of 5 truncated-and-answered, 2 timeouts, 1 `schema_invalid` | **0 / 2** |

The truncated answers are not merely degraded, they are void. Having lost the head of the
prompt (the instructions and the `CANDIDATES` block), the model invents the identifiers:

```
invented signal_ids: "signal_0", "signal_1", "signal_2"
                     "TRUMP/USDT", "TUT/USDT", "WIF/USDT"
real signal_ids    : "sig-20260925T0100Z-mkt-news_event", "sig-20260925T0100Z-bch-move"
```

Host verification (`screener.verify`) then discards every item — 86 of 86 across five qwen
calls. `_apply_score` treats a dropped item as `screen_unavailable(...)` and lets the
**detector score stand alone**, so a screener in this state does not filter, it abstains.

### And it did happen in production, before the guard existed

`journal/journal.db: llm_calls` has four `task=scan`, `status=ok` rows on `llama3.1:8b`,
and every one of them records `input_tokens = 4098`:

```
2026-09-23T17:26:29Z  llama3.1:8b  ok  44504 ms  in=4098  out=172
2026-09-23T17:29:33Z  llama3.1:8b  ok  43016 ms  in=4098  out=172
2026-09-23T17:30:16Z  llama3.1:8b  ok  30573 ms  in=4098  out=172
2026-09-23T17:30:47Z  llama3.1:8b  ok  30761 ms  in=4098  out=172
```

4,098 is exactly the value this reproduction produces, for every oversized prompt, whatever
its real size — Ollama keeps half of `num_ctx` (4,096) plus two special tokens and drops the
rest. Five qwen calls on prompts of 19,253–28,300 tokens all reported `prompt_eval_count =
4098`, and two llama calls did too.

Being careful about what that proves: four calls four minutes apart would legitimately share
one prompt size, and all four also report an identical `output_tokens = 172`, so the repeated
value is not by itself evidence of truncation. What is evidence is that the value is *exactly
the truncation ceiling* for `num_ctx: 8192`; that the same task on the same code renders
19,253–30,704 tokens; and that four hours later the context guard began refusing the very
same prompt as too large. The journal still cannot distinguish "genuinely 4,098 tokens" from
"truncated to 4,098", because nothing records the pre-truncation size — worth fixing on its
own account, since it is the one number that would make this unambiguous.

### What the journal actually shows: two eras, both bad

`provider_switches` dates the transition precisely. The context guard's first
`skipped_capability` for `scan` is **2026-09-23T21:46:54Z**, four hours after the last
truncated `ok`. Scan calls by provider, by day:

| day | `ollama` | `claude:subscription` |
|---|---|---|
| 2026-09-23 | 8 (4 `ok` at 4,098 tokens, 4 `timeout`) | 5 |
| 2026-09-24 | **0** | 8 |
| 2026-09-25 | **0** | 4 |

So there were two regimes, and neither one screens:

* **Before the guard** — Ollama silently truncated and the model answered about input it
  never saw. Four rows say `ok`.
* **After the guard** — `fits_context` fires correctly and is journaled, on **13 of 13
  cycles**, with an honest reason:
  `{"model": "local_small", "why": "prompt ~15,861 tokens does not fit max_ctx 8192 with
  1024 reserved for the answer"}`. The local model is skipped and every scan runs on Haiku.

The guard is working. It did not fail; it turned a silent wrong answer into a silent
full-price escalation. **`local_small` has served zero scan calls since 2026-09-23**, and
`tasks.scan` — the task whose entire justification is *"local-first because it is high
volume"* — is running exclusively on the cloud model against `monthly_budget_usd: 3`.

Live traffic during this eval (nine `llm_calls` rows written by cron, none by the eval —
it passes `jdb=None` and no row carries its run id) shows the steady state, plus two
independent confirmations of §8:

```
2026-09-24T23:01:09Z  scan  claude-haiku-4-5  ok       44415 ms
2026-09-24T23:01:53Z  scan  claude-haiku-4-5  error    28844 ms   <- max_turns (2) exhausted
2026-09-25T00:00:05Z  scan  claude-haiku-4-5  timeout  95538 ms   <- deadline_s 90 breached
2026-09-25T00:00:54Z  scan  claude-haiku-4-5  ok       80659 ms
2026-09-25T01:00:54Z  scan  claude-haiku-4-5  ok       48965 ms
2026-09-25T01:01:43Z  scan  claude-haiku-4-5  ok       38399 ms
```

Two of six live scan calls failed, one on `deadline_s: 90` and one on
`Reached maximum number of turns (2)` — so on the full prompt, `max_turns: 2` is not always
enough for scan either (4 `classify` rows have the same error, from before that fix).

### The guard is right but its arithmetic is not

That matters because the guard's number is also the only thing that could tell you what
`num_ctx` *would* be enough — and it is 1.8× too low.
`runs/llm/chain.py: fits_context` is documented as "deliberately pessimistic". On this
payload it is optimistic:

| prompt | `estimated_tokens` (chars / 3.5) | real tokens (p50) | ratio | real max |
|---|---|---|---|---|
| scan, prod, granite | 10,865 | 20,024 | **1.84×** | 28,300 |
| scan, prod, qwen | 10,865 | 20,887 | **1.91×** | 30,704 |
| scan, prod, llama | 10,824 | 17,351 | 1.60× | 17,351 |
| scan, lean, granite | 2,501 | 3,896 | 1.55× | 4,798 |
| scan, lean, qwen | 2,501 | 4,017 | 1.60× | 5,241 |

`CHARS_PER_TOKEN = 3.5` is a reasonable figure for prose. The scan prompt is not prose: it
is 55–85% dense JSON of the form `"BTC/USDT.rsi_4h":52.1`, which tokenizes at roughly
2 characters per token.

The practical consequence is a trap. The journal says *"prompt ~15,861 tokens does not fit
max_ctx 8192"*, which reads like an invitation to set `num_ctx: 16384`. The real count is
~28,000, so that change would replace `skipped_capability` with an
`exceed_context_size_error` on granite, a silent truncation on qwen, and a grind on llama —
exactly the three failures of the table above. **Do not size `num_ctx` from this number.**

---

## 2. Where the tokens go, and the prompt that would fit

Composition of the `prod` scan prompt at batch 5 (estimated tokens, p50 over the corpus):

| block | news-rich window (n=6) | quiet window, 0 news (n=32) |
|---|---|---|
| `CANDIDATES` — the 5 things being judged | 580 (3.5%) | 588 (5.4%) |
| `FEATURES` — 1,029 keys over 107 pairs | 9,171 (55.4%) | 9,139 (84.6%) |
| `NEWS` — up to 60 items, `indent=2` | 5,724 (34.6%) | 1 (0.0%) |
| **total** | **16,562** | **10,802** |

The model is asked about five candidates and shown 107 pairs. The five candidates name six
pairs between them.

So I costed a second prompt regime, **`lean`**: the same template, the same code path, but
`FEATURES` restricted to the pairs the batch actually names plus core, and `NEWS` to the
hashes the candidates cite plus the five newest. Median 6 pairs instead of 107.

| regime | est tokens over all 50 snapshots (min / p50 / max) | real tokens (p50) | real max seen |
|---|---|---|---|
| `prod` | 10,584 / 10,818 / 16,678 | 20,024 | **30,704** |
| `lean` | 2,026 / 2,395 / 3,229 | 3,896 | **5,241** |

`lean` renders 3–7 pairs (p50 6) instead of 107, and fits 8192 with room for the
1,024-token output reserve. `prod` does not fit 8192, 16384, or — in any useful sense —
32768 either (§4).

`lean` is a **hypothesis I measured, not shipped code.** Producing it means changing
`runs/signals/screener.render_prompt` (tier 2). Every screening number below is reported
for both regimes so the cost of not doing it is visible.

---

## 3. Latency, on the `lean` prompt, at two host loads

The host was carrying other work for the first half of this eval (loadavg 29–48 on 12
cores: two `ml.train_cpu` runs, six pytest suites, three `an1/sig2.py` jobs). Everything
was re-measured after it went quiet. Load is recorded with every row.

| model | host | n | p50 | p95 | gen rate | prompt tok | out tok | calls > 90s | loadavg |
|---|---|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | loaded | 10 | **12.2s** | 29.8s | 61.1 tok/s | 3,825 | 610 | 0/10 | 32.6 |
| `granite4.2:3b` | quiet | 6 | **12.1s** | 25.4s | 62.4 tok/s | 4,033 | 614 | 0/6 | 4.1 |
| `qwen3.5:4b-q4_K_M` | loaded | 10 | **12.3s** | 29.3s | 47.6 tok/s | 3,932 | 434 | 0/10 | 32.7 |
| `qwen3.5:4b-q4_K_M` | quiet | 6 | **12.0s** | 26.3s | 49.2 tok/s | 4,139 | 419 | 0/6 | 3.7 |
| `llama3.1:8b` | loaded | 10 | 86.8s | 150.6s | 5.2 tok/s | 3,341 | 390 | **5/10** | 34.0 |
| `llama3.1:8b` | quiet | 6 | 58.6s | 121.0s | 7.1 tok/s | 3,528 | 397 | **1/6** | 3.8 |
| `haiku` (cloud) | loaded | 8 | 79.7s | 110.8s | — | — | — | 2/8 | 32.7 |

### Host contention explains part of it, and only for llama

This was the specific question raised mid-eval, and the answer is clean. Between loadavg
~33 and loadavg ~4:

* `granite4.2:3b` 12.2s → 12.1s. **No change.**
* `qwen3.5:4b-q4_K_M` 12.3s → 12.0s. **No change.**
* `llama3.1:8b` 86.8s → 58.6s. **1.48× faster**, 5.2 → 7.1 tok/s.

The mechanism is GPU residency, from `/api/ps`:

| model | size | in VRAM | on GPU |
|---|---|---|---|
| `granite4.2:3b` | 2.92 GB | 2.92 GB | **100%** |
| `qwen3.5:4b-q4_K_M` | 3.27 GB | 3.27 GB | **100%** |
| `llama3.1:8b` | 6.15 GB | 4.19 GB | **68%** |
| `nomic-embed-text` | 0.32 GB | 0.32 GB | 100% |

Roughly a third of `llama3.1:8b` runs on the CPU, which is why it is both slow and the only
one whose speed moves with WSL load. The two smaller models are entirely GPU-resident and
are effectively immune to it.

So: contention was a real ~1.5× tax on `llama3.1:8b` and no tax at all on the alternatives.
It does **not** explain the 5–7× gap between them — that gap is VRAM fit, and it is stable
across load. The earlier throwaway figures (7.4 / 49 / 66 tok/s) turn out to be close to the
quiet-machine numbers (7.1 / 49.2 / 62.4 tok/s); the ranking and roughly the magnitudes
survive.

### Two other latency terms, both larger than the model choice

**Model load, 4–19s.** `load_duration` was 0 on most calls but 4.4–18.6s whenever a model had
been evicted (and 28.0s on a cold start measured separately, where loading was 28.0s of a
28.5s call). The GPU holds ~6 GB; `granite` (2.92) + `qwen` (3.27) = 6.19 GB cannot
both be resident, and `nomic-embed-text` is also wanted by the watcher. Configuring two
different local models guarantees eviction thrash, because the scanner runs every 5 min and
the watcher every 7 min. **Use one local model for all three local tasks.**

**Thinking tokens, 4×.** `granite4.2` and `qwen3.5` are reasoning models and Ollama turns
thinking **on by default**. `runs/llm/providers/ollama.py` never sends `think`, and it reads
only `message.content` — so the reasoning trace is generated, paid for in wall time, and
discarded. Quiet host, classify at batch 25:

| model | `think` | n | ok | p50 | output tokens (p50) |
|---|---|---|---|---|---|
| `granite4.2:3b` | off | 3 | 3 | **32.2s** | 1,762 |
| `granite4.2:3b` | default | 3 | 2 | **131.6s** | 7,498 (1 timeout) |
| `qwen3.5:4b-q4_K_M` | off | 3 | 3 | **39.2s** | 1,761 |
| `qwen3.5:4b-q4_K_M` | default | 3 | **0** | — | — (**3× `empty_output`**) |

With thinking on, `granite` is 4.1× slower for an identical answer and `qwen` returns
**nothing at all** — `message.content` comes back empty and the provider raises
`empty_output`. That is the whole explanation of "qwen kept ZERO candidates": it was never
a screening judgement, it was an empty string. It also explains the production rows
`scan | qwen3.5:4b-q4_K_M | timeout | 145–149s` against a 90s timeout — a first `_chat`
returning unusable output, then the schema-repair `_chat` hitting the timeout, with the
router's `latency_ms` covering both.

Every number in this document for a local model is with `think: false`, which is a one-line
change to `runs/llm/providers/ollama.py`. Without it, no local model is viable for anything.

---

## 4. The real prompt, in a window big enough to hold it

For completeness: `prod` at `num_ctx: 32768`, quiet host.

| model | n | ok | p50 wall | prompt tok (p50) | prompt-eval | gen rate | host-kept |
|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | 6 | 6 | **143.3s** | 20,024 | 20.7s | 5.1 tok/s | 20/28 |
| `qwen3.5:4b-q4_K_M` | 6 | 6 | **47.1s** | 20,887 | 20.2s | 16.2 tok/s | 29/30 |
| `llama3.1:8b` | 2 | 1 | **261.1s** | 17,351 | 31.1s | 1.6 tok/s | 4/4 (1 timeout at 600s) |
| `haiku` (cloud) | 8 | 8 | 62.8s (p95 141.2s) | — | — | — | 40/40 |

Two things to take from this. First, prompt evaluation alone is ~20s — the fixed price of a
20k-token prompt before a single output token exists. Second, generation collapses with
context: `granite` goes from 62 tok/s at 4k to **5.1 tok/s at 20k**, a 12× degradation, as
the KV cache pushes past the 6 GB GPU. `qwen3.5` degrades far more gracefully (49 → 16
tok/s) and is the only local model that fits the real prompt inside `deadline_s: 90` — at
47s p50, with no margin.

Raising `num_ctx` is therefore not a fix. It converts a loud failure into a 143s success
on a task whose deadline is 90s, and it costs memory that the GPU does not have. **Shrink
the prompt instead.**

---

## 5. Screening quality

`lean` prompt, batch 5, loaded and quiet runs pooled — 80 items per local model (from 16
snapshots, of which 2 overlap between the two runs, so ~70 distinct items) and 40 for Haiku.
"hostDrop" is `screener.verify` discarding an item. Agreement is on the
`screened` / `screened_out` decision `_apply_score` would actually reach, against Haiku on
the byte-identical prompt.

| model | calls | items | schema OK | repairs | hostDrop | keep | distinct scores | agree | wasted escalation | **missed signal** |
|---|---|---|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | 16 | 80 | 16/16 | 0 | 22 (**28%**) | 90% | 32 | 80% | 10% | **10%** |
| `qwen3.5:4b-q4_K_M` | 16 | 80 | 16/16 | 0 | 10 (**12%**) | 62% | 23 | 85% | 15% | **0%** |
| `llama3.1:8b` | 16 | 80 | 16/16 | 0 | 0 (**0%**) | 59% | 9 | 85% | 5% | **10%** |
| `haiku` (cloud) | 8 | 40 | 8/8 | 0 | 0 | 80% | 26 | — reference — | | |

**Schema compliance is a solved problem.** All 48 local calls on this prompt returned JSON
that validated against `schemas/signal_screen.json` on the first attempt, with zero repair
retries. Ollama's `format` field does its job. Across the entire eval there were exactly five
calls that needed the provider's repair retry or failed schema outright, and four of the five
were truncated `prod` prompts; the only one on a `lean` prompt was `qwen3.5` at batch 10
(truncated JSON, 1 of 6).

**The "keeps everything / keeps nothing" hypothesis is refuted.** Given a prompt the model can
actually see, and `think: false`, keep rates are granite 90%, Haiku 80%, qwen 62%, llama 59%.
None of the three is degenerate, and two of the three are more selective than Haiku. The
earlier throwaway result — qwen keeping zero, granite keeping 19 of 20 — was the thinking
artefact of §3 on one side and, on the other, granite's genuine tendency to be permissive.

**Agreement is 80–85% for all three**, and the asymmetry is the interesting part. `qwen3.5`
never dropped anything Haiku would have kept (0% missed signals) at the price of a slightly
higher wasted-escalation rate; `granite` and `llama` each missed 2 of 20. At n=20 per model
the difference between 0% and 10% is two items, so this ranks the models weakly at best.

### The invented feature keys are not the model's fault

This is the most actionable finding in the eval. Across **every non-truncated call — 84
calls and 390 screened items** — every invented citation of any kind is the same one key:

```
granite4.2:3b  156 items   invented keys: {'high_30d': 37}   invented news hashes: 0
qwen3.5:4b     150 items   invented keys: {'high_30d': 13}   invented news hashes: 0
llama3.1:8b     84 items   invented keys: {}                 invented news hashes: 0
```

`runs/signals/detectors.py: dip_from_high` fires on `dip_from_high_pct`, which is a
**cheap-tier** key computed for all 107 watchlist pairs, and then declares the candidate's
`feature_keys` as `(<pair>.dip_from_high_pct, <pair>.high_30d)`. But `high_30d` is a
**rich-tier** key, computed only for the 20 rich pairs. So the `CANDIDATES` block shows the
model `TRUMP/USDT.high_30d` and the `FEATURES` block does not contain it.

Over the whole corpus, **146 of 788 (18.5%) of the feature keys named in `CANDIDATES` are
absent from `FEATURES`, and all 146 are `dip_from_high`'s `high_30d`.**

A model that does exactly what the prompt demands — *"quoted by its exact key"* — has its
entire item discarded and is counted as a hallucination. `granite`'s 28% drop rate is this
bug; so is `qwen`'s 12%.

`llama3.1:8b` scores 0% not because it is more faithful but because it barely cites
anything. Valid feature-key citations on the `lean` prompt, 80 items each:

| model | valid key citations | per item | items citing nothing at all |
|---|---|---|---|
| `haiku` (40 items) | 82 | **2.05** | 2 |
| `granite4.2:3b` | 98 | 1.23 | 4 |
| `qwen3.5:4b-q4_K_M` | 90 | 1.12 | 4 |
| `llama3.1:8b` | 68 | **0.85** | **14** |

The prompt says *"cite nothing rather than guess"*, so llama is not breaking a rule — but an
item with no evidence behind it is worth less to the validator, and 14 of its 80 items had
none.

Outside this one key, across all 390 screened items, **no local model invented a single
feature key, and not one invented a news hash.** Faithfulness is not the differentiator
anyone expected it to be.

### Score calibration and the gray-zone bill

`signals.scanner.screen.gray_zone: [0.45, 0.65]` re-runs a batch on the cloud model. How
often each screener triggers that, on the `lean` prompt:

| model | host-kept items | in gray zone | **batches needing a paid cloud re-run** |
|---|---|---|---|
| `granite4.2:3b` | 57 | 7 (12%) | **6/16 (38%)** |
| `qwen3.5:4b-q4_K_M` | 70 | 19 (27%) | **9/16 (56%)** |
| `llama3.1:8b` | 80 | 23 (29%) | **13/16 (81%)** |
| `haiku` | 40 | 15 (38%) | 6/8 (75%) |

`llama3.1:8b` emits only 9 distinct scores over 80 items — effectively `{0.0, 0.2, …, 0.9}` —
and 0.5/0.6 sit inside the gray band, so four of five of its batches escalate. `granite`
escalates least. At the measured $0.065 median per Haiku `lean` re-run, `tasks.scan`'s
`monthly_budget_usd: 3` buys about **46 escalations a month**; granite's 38% rate exhausts
that at roughly four screened batches a day, llama's 81% at under two. The gray band deserves
its own look — it is a bigger lever on scan cost than the model is.

### Batch size (quiet host, `lean`)

| model | batch | p50 | per item | hostDrop | prompt tok |
|---|---|---|---|---|---|
| `granite4.2:3b` | 1 | 3.2s | 3.20s | 0/6 | 2,235 |
| `granite4.2:3b` | 5 | 12.1s | **2.42s** | 8/30 | 4,033 |
| `granite4.2:3b` | 10 | 15.0s | **1.50s** | 8/42 | 4,701 |
| `qwen3.5:4b-q4_K_M` | 1 | 4.4s | 4.40s | 0/6 | 2,295 |
| `qwen3.5:4b-q4_K_M` | 5 | 12.0s | **2.40s** | 1/30 | 4,139 |
| `qwen3.5:4b-q4_K_M` | 10 | 15.0s | **1.50s** | 2/34 | 4,777 (1 `schema_invalid`) |

Per item, bigger is cheaper, because the fixed `FEATURES`/`NEWS` blocks amortise. But the
gray zone is evaluated per batch: one ambiguous item re-runs all ten on the cloud model. At
batch 10 qwen also produced its only malformed JSON of the eval. **Keep
`max_candidates_per_cycle: 5.`** It pays ~60% more per item than batch 10 and in exchange
bounds the blast radius of a single gray score to five candidates instead of ten.

(Aside, not a model question: 6–15 candidates fire per cycle, p50 8, and only 5 are ever
screened. The remainder sit at `status=candidate` on their detector score until they expire.)

---

## 6. Classification quality

Two references were used, because the obvious one turns out to be broken.

### The `rule` labels are not ground truth

`tasks.classify` has `on_all_failed: rule`, and 88 rows in `knowledge/earn.db` carried
`classified_by='rule'` with an event class at the time of sampling (the DB is live; ingest
adds rows every 15 minutes). Those labels are **substring** matches. `lawsuit: [sec, lawsuit,
charges, settle]` matches the "sec" inside **settlement, seconds, secretary, Security,
securities, second**:

```
rule=lawsuit  "Bitcoin's $16 billion quarterly options settlement arrives..."
rule=lawsuit  "Solana starts testing upgrade that could cut finality from 12.8 seconds..."
rule=lawsuit  "US Treasury secretary emerges as frontrunner for Trump's AI czar role"
rule=lawsuit  "UN Security Council Will Get Advice on AI Risks From Tech Giants"
rule=lawsuit  "Europe's Central Bank Prepares to Invest Own Funds in Tokenized Securities"
```

I read all 88 headlines and labelled them against the configured vocabulary, applying two
rules mechanically (SEC/CFTC regulatory news → `lawsuit`; Fed *monetary policy* → `macro`,
Fed bank-supervision minutiae → `null`). **The keyword rule agrees with that adjudication on
45 of 88 = 51%**, and 41 of its 43 errors are false positives — it asserts an event class
where there is none (`lawsuit`→`null` 20, `macro`→`null` 17, `hack`→`null` 3,
`upgrade`→`null` 1).

Two consequences. First, "accuracy against the rule label" cannot rank models — a model that
correctly answers `null` scores zero. Haiku scores 38–77% against it depending on batch size,
which says more about the reference than about Haiku. Second, and more
seriously, **`on_all_failed: rule` is not a safe floor for classify.** Half of what it
asserts is wrong, and `signals.scanner.detectors.news_event` fires on
`{hack, depeg, delist, lawsuit, outage}` with `fast_path_events: [hack, depeg, delist]`.
That is a separate bug from this decision, but it is in the blast radius of it.

Everything below is scored against the **analyst adjudication**. `think: off` throughout.
Loaded and quiet runs are pooled; at `temperature: 0` the local models returned identical
labels in both, so the `labels` column double-counts and the **effective sample is the 88
headlines**, not 176.

### Batch size 5

| model | calls | p50 | per item | labels | **exact** | false positive | missed event | wrong class | cost/call |
|---|---|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | 16/16 | **6.3s** | 1.26s | 80 | **79%** | **1%** | 20% | 0% | free |
| `qwen3.5:4b-q4_K_M` | 16/16 | 8.4s | 1.68s | 80 | 68% | **30%** | 2% | 0% | free |
| `llama3.1:8b` | 16/16 | 29.0s | 5.80s | 80 | **80%** | 2% | 15% | 2% | free |
| `haiku` (cloud) | 8/8 | 17.0s | 3.40s | 40 | **90%** | 2% | 5% | 2% | ~$0.022 |

### Large batches — what `runs/ingest.py` actually sends (`pending[:25]`)

The 88 headlines split into batches of 25, 25, 25 and 13, so "per item" below is p50 wall
divided by 25 and is therefore a slight over-estimate.

| model | calls | p50 | per item | labels | **exact** | false positive | missed event | wrong class |
|---|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | 8/8 | 32.1s | 1.28s | 176 | 62% | **0%** | 38% | 0% |
| `qwen3.5:4b-q4_K_M` | 8/8 | 40.2s | 1.61s | 176 | 68% | 19% | 12% | 0% |
| `llama3.1:8b` | **1/4** | 100.9s | 4.04s | 13 | 46% | 0% | 54% | 0% |
| `haiku` (cloud) | 3/4 | 46.9s | 1.88s | 63 | 81% | 6% | 10% | 3% |

Three conclusions.

**Batch 25 costs accuracy for every model.** granite 79% → 62%, Haiku 90% → 81%. Per-item
wall time is flat (1.26s vs 1.28s for granite), so the large batch buys nothing and loses
17 points of accuracy. Long lists make these models skip items rather than mislabel them:
granite's miss rate goes 20% → 38%.

**`llama3.1:8b` cannot serve `classify` at the production batch size.** 3 of 4 batches timed
out at 240s; `tasks.classify.deadline_s` is **120**. A 25-item batch needs ~1,760 output
tokens, which at 7 tok/s is ~250s. It survives in production today only because live batches
have been 2–8 items (`input_tokens` 397–672, `output_tokens` 33–134 in `llm_calls`) — and it
still timed out twice, at 74.9s and 114.3s.

**The error *shapes* differ, and that decides it.** `granite` is conservative: 0–1% false
positives, misses instead. `qwen3.5` is the opposite: **30% false positives** at batch 5. For
a label that feeds `news_event` — where a spurious `hack` triggers `fast_path` and a
`research_run` — a false positive is the expensive error and a miss is merely a loss. On this
axis granite is clearly preferable, and it is 4.6× faster than the equally accurate
`llama3.1:8b`.

---

## 7. `holdings_watch`

Reconstructed from the 8 real `watch_events` rows (prompt sizes within 1% of the recorded
`prompt_chars`). Quiet host, `think: off`.

| model | n | ok | schema OK | p50 | p95 | states | confidence | dropped citations |
|---|---|---|---|---|---|---|---|---|
| `granite4.2:3b` | 8 | 8 | **8/8** | **0.9s** | 5.0s | all `intact` | 1.0 | 0 |
| `qwen3.5:4b-q4_K_M` | 8 | 8 | **8/8** | 1.2s | 6.8s | all `intact` | 1.0 | 0 |
| `llama3.1:8b` | 8 | 8 | **8/8** | 2.9s | 9.5s | all `intact` | 1.0 | 0 |

At ~730 prompt tokens this task is easy for all three, and it is the one place where the
incumbent is adequate.

One correction to a comment in `config/models.yaml` and `runs/watch/schema.py`: they state
that `qwen3.5:4b` "answers 0-100 where the schema says 0-1, 0 of 3 valid". **Not
reproduced** — with Ollama's `format` constraint and `think: false`, qwen was 8/8
schema-valid with `confidence: 1.0`. That claim should not be used to rule qwen out.

This is the weakest part of the eval and it should not carry weight. Only two distinct
positions have ever existed, both were recorded with `news_shown: 0` and no stored thesis,
so the model was asked whether nothing contradicts nothing. `intact` is trivially right. It
measures schema compliance and latency, not judgement.

---

## 8. Exact configuration to set

The invariant holds: `capabilities.local_small.max_ctx` **equals**
`providers.ollama.options.num_ctx`, and the recommendation is to leave both at **8192**
rather than raise them — §4 shows that raising the window converts a correctly journaled skip
into a 143s "success" on a task whose deadline is 90s, and the memory is not there anyway.

**Read this before making the change:** swapping the model alone changes nothing for `scan`.
`local_small` is skipped on every cycle because the prompt does not fit (§1), and it will go
on being skipped whichever model sits behind the alias. The model choice only takes effect
once change (2) below lands. It takes effect immediately for `classify` and
`holdings_watch`, whose prompts already fit.

```yaml
models:
  local_small: { provider: ollama, id: "granite4.2:3b", tier: 1 }   # was llama3.1:8b

capabilities:
  local_small: { tools: false, skills: false, structured_output: true, max_ctx: 8192 }

providers:
  ollama:
    options: { temperature: 0, num_ctx: 8192 }   # unchanged; must equal max_ctx above
    keep_alive: "10m"                            # unchanged

tasks:
  scan:
    deadline_s: 180        # was 90  -- set by the haiku escalation, not the local model
    max_turns: 4           # was 2   -- one live scan call exhausted 2
  classify:
    deadline_s: 120        # unchanged
  holdings_watch:
    deadline_s: 90         # unchanged
```

**Why `granite4.2:3b`.** One model must serve all three local tasks (the 6 GB GPU cannot
hold two, and eviction costs 4–19s per swap). granite wins `classify` decisively — 79% exact
against 68% for qwen, with 1% false positives against 30%, at 6.3s against 8.4s — and it ties
qwen on `lean` screening latency (12.1s vs 12.0s) and is fastest on `holdings_watch`. Its one
weakness, the 28% host-drop rate, is the `dip_from_high` bug of §5 and not a property of the
model.

**`qwen3.5:4b-q4_K_M` is the runner-up and the right choice in exactly one case:** if change
(2) is not going to happen and you want *something* local screening, it is the only local
model that completes the real 20k-token prompt inside 90s (47.1s p50, 29/30 items
host-verified), because it degrades far better with context. That path needs **both**
`num_ctx` and `max_ctx` moved to 32768 together — the invariant is not optional, and setting
one without the other either skips work the model could do or hands it work it cannot. It
also costs you the classifier: qwen's 30% false-positive rate on `classify` is the error shape
that feeds a spurious `hack` into `news_event`'s fast path. Slimming the prompt is the better
trade in every respect.

**`llama3.1:8b` should be retired from the local tier.** It is 5–7× slower than either
alternative at the same quality, because a third of it runs on the CPU; it breaches
`deadline_s: 90` on 5 of 10 `lean` scan calls under load and 1 of 6 when quiet; it cannot
complete a 25-item `classify` batch inside 240s, against a configured 120; and its 9 distinct
scores put 81% of screened batches into the gray zone, making it by some distance the most
expensive local model to run.

**`deadline_s: 180` for scan** is set by the *cloud escalation*, not the local model. granite's
`lean` p95 is 25.4s, but the gray-zone re-run goes to Haiku, whose `lean` p95 is 110.8s, and
Haiku already breaches the current 90s on 2 of 8 `lean` and 3 of 8 `prod` calls — during this
eval `tasks.scan.deadline_s` had to be widened in memory just to collect a reference set at
all. With a ~25s local call first, 180s for the re-run still fits inside
`signals.scanner.deadline_s: 240`, and the router clamps to whichever is tighter anyway.

### Three changes that matter more than the model (all tier 2, human-only)

1. **`runs/llm/providers/ollama.py`: send `"think": false`** in the `/api/chat` payload.
   Without it `qwen3.5` returns `empty_output` on 3 of 3 batch-25 classify calls and
   `granite` pays 4.1× in wall time for a reasoning trace the provider discards. This is the
   single highest-value line in the eval, and it applies whichever model is chosen.
2. **`runs/signals/screener.render_prompt`: slim the prompt.** 20,024 → 3,896 real tokens at
   the p50 by rendering only the pairs the batch names plus core, and capping `NEWS`. Without
   this, no local model can screen at all (§1). This is the "Python pre-filters, the model
   ranks a short list" shape, and it is what makes an 8192 window sufficient.
3. **`runs/signals/detectors.py: dip_from_high`: stop declaring `<pair>.high_30d`** on a
   cheap-tier pair, or compute `high_30d` for every pair the detector can fire on. 18.5% of
   the feature keys shown in `CANDIDATES` do not exist in `FEATURES`, all of them this one,
   and they account for every "hallucination" the screener has ever counted.

### Also worth a look, surfaced by this eval but not part of the decision

* `runs/llm/chain.py: CHARS_PER_TOKEN = 3.5` under-counts this payload by 1.55–1.91×.
  **2.0** matches the measured ratio almost exactly on both regimes (56,516 chars / 2.0 =
  28,258 vs 28,300 real), and a guard that exists to prevent silent truncation should not be
  optimistic. Better still, take the count from Ollama.
* `runs/ingest.py: classify_news` sends `pending[:25]`. **5–8** is both faster per item and
  17 accuracy points better for every model tested. The batch size is a hard-coded literal;
  it should be a config key.
* `on_all_failed: rule` for `classify` falls back to labels that are 51% correct (§6). The
  substring match on `"sec"` alone produces 20 false `lawsuit` labels out of 88.
* Nothing records a prompt's size *before* Ollama truncates it, so an `ok` row with
  `input_tokens = 4098` is indistinguishable from a genuine 4,098-token prompt. Journalling
  the rendered prompt's real token count on every local call would have made §1 a one-minute
  question instead of a reproduction.
* `tasks.scan.max_turns: 2` is not always enough: one of six live scan calls during this eval
  failed with `Reached maximum number of turns (2)`. The same error appears on 4 `classify`
  rows from before that value was raised to 2.
* `signals.scanner.screen.gray_zone: [0.45, 0.65]` puts 38–81% of batches on the cloud model
  depending on the local model's score granularity, against a $3/month scan budget.
* 6–15 candidates fire per cycle (p50 8) and `max_candidates_per_cycle: 5` means the rest are
  never screened at all.

---

## 9. Method, and what this eval cannot tell you

**Corpus.** 50 scanner snapshots from real data: `knowledge/earn.db` copied to scratch, the
1h/4h/1d feathers in `data/binance/` loaded back to 2025-09-01, then candles and news deleted
monotonically backwards to each cut-off so every snapshot sees only what existed then. 14
cut-offs inside the live news window (real `NEWS` blocks, up to 60 items) and 36 over the
previous year (quiet news windows). Prompts rendered by `screener.render_prompt` itself, with
`features.build` and `detectors.run_all` unmodified. Classify prompts rendered through the
real template with the real `classify_schema`. Watch prompts reconstructed from recorded
`facts_json` to within 1% of the recorded `prompt_chars`.

**Execution.** Local calls go through the real `OllamaProvider` (including its schema-repair
retry); the only deviation is the `think` field. Cloud calls go through the real
`runs.llm.chain.run_task` with `pin="haiku"`, under `ops/envwrap.sh research`, with
`jdb=None` / `kdb=None` so the measurement adds no `llm_calls` rows, moves no circuit breaker
and consumes no production budget. Answers are checked with the production
`schemas.signals.validate_screen` and `screener.verify`. Nothing was written to `~/earn-run`;
no config file was edited; no bot, cron job or running process was touched or killed other
than my own probes.

**Cloud spend: $2.0655 over 32 Haiku calls** (31 succeeded) — $0.6007 on 16 classify calls,
$1.4649 on 16 scan calls. Per call, median (min–max):

| Haiku call | n | median | range |
|---|---|---|---|
| classify, batch 5 | 8 | $0.0218 | $0.018 – $0.030 |
| classify, batch 25, `truth` set | 3 | $0.0409 | $0.027 – $0.064 |
| classify, batch 25, `prod` set | 4 | $0.0734 | $0.057 – $0.082 |
| scan, `lean` prompt | 8 | $0.0651 | $0.037 – $0.076 |
| scan, `prod` prompt | 8 | $0.1139 | $0.097 – $0.162 |

These are the router's own cost figures and they include the Claude Code SDK's system-prompt
overhead, which dominates a small call — a 1,000-token classify batch still costs ~2 cents.

**Raw data.** Every call, with its prompt tokens, Ollama timings, contemporaneous load average
and host-verification outcome, is one JSON line under `~/tmp/le/` in WSL:
`scan_local.jsonl` / `quiet_scan.jsonl`, `cls_local.jsonl` / `quiet_cls.jsonl`,
`quiet_watch.jsonl`, `cloud_scan.jsonl`, `cloud_classify.jsonl`, with the corpus in
`scan2.json` / `classify_samples.json` / `watch_samples.json` and the adjudication in
`adjudicated.json`. These are outside the repo and outside any backup — copy them if the
numbers need to be re-derived.

### Limits, honestly

* **The `lean` prompt is a hypothesis, not code.** Its numbers say what a slimmed prompt
  would cost, not what today's code does. Whether a 6-pair `FEATURES` block screens *as well*
  is untested — rule 5 of `scan.v2.md` exists precisely because absence of a key is not
  evidence, and cutting 101 pairs removes context the model might have used. The agreement
  numbers in §5 compare local models against Haiku **on the same lean prompt**, so they are
  valid relative measures; they do not prove `lean` ≈ `prod` in quality. Haiku on `prod`
  kept 88% against 80% on `lean`, which is weak evidence the fuller prompt is worth something.
* **Agreement with Haiku is a proxy, and n is small** — 20 decisions per model. The 0% vs 10%
  missed-signal split between qwen and the others is two items. Do not treat it as
  significant. There is no outcome ground truth for screening at all: `signal_validations` has
  8 rows and `signals` 39, so nobody yet knows which screened signals were *right*.
* **Classify ground truth is mine.** 88 headlines adjudicated by one reader (me) in one pass,
  with no second opinion and no inter-rater agreement. Two judgement calls — SEC commentary →
  `lawsuit`, Fed supervision minutiae → `null` — move several points of accuracy. The
  distribution is also skewed: 41 of 88 are `null` and `delist`, `depeg` and `outage` never
  appear, so nothing here says anything about the three event classes that matter most to the
  risk gate.
* **`holdings_watch` is barely measured.** Two positions, no news, no thesis (§7).
* **Load is only half-controlled.** Screening and classify quality are load-independent by
  construction, and the two 3–4B models proved load-independent in latency too. But
  `llama3.1:8b`'s quiet figures (58.6s p50) come from n=6, and a machine at loadavg 0.35 with
  nothing else touching the GPU is not the machine the scanner runs on at 08:30 alongside
  ingest and the watcher. Treat 12s for granite as reliable (it held at loadavg 32.6 and 4.1)
  and llama's numbers as a lower bound on its latency.
* **One host, one GPU, one day.** Every conclusion about VRAM residency, context degradation
  and load sensitivity is specific to this ~6 GB GPU. On a 24 GB card `llama3.1:8b` would be
  fully resident and much of §3 would change.
* **Not measured:** whether `granite4.2:3b` is stable across Ollama upgrades; any model not
  already pulled on this host; whether a quantisation change (`granite4.2:3b-q8`, a smaller
  `llama`) would alter the ranking; the accuracy cost of the `lean` prompt on real screening
  outcomes; and `tasks.extract` and `tasks.flags`, which also list `local_small` in their
  chains but have no production traffic to build a corpus from.
