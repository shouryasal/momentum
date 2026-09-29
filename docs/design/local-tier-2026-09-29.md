# Making the local tier actually screen — measured 2026-09-29

Follow-up to [local-model-choice.md](local-model-choice.md) (2026-09-25), which ended on an
untested hypothesis: *"the scan prompt must shrink from ~20–30k tokens to ~4k; whether a
6-pair FEATURES block screens as well is untested"*. This document ships the slim prompt,
measures the hypothesis on 40 real candidate batches, and decides the model behind
`local_small`.

Everything here was run on the live host (Windows + WSL Ubuntu-24.04, Ollama at
`http://172.29.64.1:11434`, ~6 GB GPU, loadavg 5–17 throughout from unrelated work) through
the **real router** (`runs.llm.chain.run_task`, pinned, `jdb=None`/`kdb=None` so no
`llm_calls` row, breaker or budget on the runtime moved). Cloud calls went through
`~/earn-run/ops/envwrap.sh research`. Nothing under `~/earn-run` was written.

---

## 0. The answer, before the detail

1. **The lean render is shipped** (`runs/signals/screener.py: lean_features`,
   `render_prompt(lean=True)`). Same template, same CANDIDATES block, same answer schema;
   FEATURES cut to the pairs the batch names plus `universe.core`, NEWS to what those
   candidates cite or concern (cap 12 uncited items). 40 real batches: **3,529 / 5,671 /
   6,689 estimated tokens** (min / p50 / max; 5,268 / 5,673 real by granite's / qwen's
   tokenizers at the p50) against 22,525 / 26,769 / 31,846 for the full render. Every one
   fits `max_ctx: 8192` with the 1,024-token answer reserve; the context guard refused
   **0 of 80** local calls. The full render stays behind `FULL_PROMPT_ON_ESCALATION` (off)
   for the cloud second opinion.
2. **The context guard's arithmetic is fixed.** `chars / 3.5` under-counted these prompts
   by 1.45–1.66× against the tokenizers' own `prompt_eval_count`. `estimated_tokens` is now
   the larger of that prose floor and a per-word / per-digit / per-symbol piece count,
   calibrated to land at 0.90–0.999× of granite's and qwen's real counts — never below.
3. **`local_small` becomes `granite4.2:3b`** (was `llama3.1:8b`). On the lean prompt it
   is schema-valid 40/40 with zero repair retries, cites **zero invented keys and zero
   invented news hashes** over 197 items, answers in **12.0 s p50 / 20.9 s p95** at
   loadavg 8.6, and misses **2%** (3 of 195) of the items Haiku keeps on the same prompt.
   Its error is permissiveness — 22% wasted escalations — which is the cheap error (the
   validator has a daily cap and a per-asset cooldown; a missed signal has no cap).
   `qwen3.5:4b` is as fast but misses **17%** (33 of 195) of Haiku's keeps.
4. **Full vs lean on Haiku itself:** 74% per-item agreement on 70 items (14 batches).
   Where they differ, the full render mostly keeps *more* (14 full-only vs 4 lean-only) at
   1.9× the price. Slimming does change the cloud model's answer on one item in four,
   toward strictness; whether the extra keeps were better trades is unknowable today (no
   validation has ever been stored — see 5). So the escalation is handed the lean render
   too, and the full one is a flag to revisit with a month of stored validations.
5. **Holdings watch does run end-to-end** with the local model at the head (schema-valid,
   citations verified, 0.7–1.2 s warm), but today it watches **nothing**: both sleeves have
   held no position since 2026-09-23 21:58Z. And when it did watch, it had no thesis to
   watch against — because **11 of the 15 validations ever run were thrown away host-side
   for citing pack paths that exist** (`market_state.data_fresh`, `limits.max_weight`).
   That is fixed here (`validator.citable_keys`). Three more causes sit outside this
   package's file ownership and are listed in §6.

Cloud spend for the measurement: **$4.7033** over 53 Haiku calls, all successful
(budget $5; $2.8532 on 39 lean calls, $1.8500 on 14 full calls, each run stopped by its
own cap).

---

## 1. Prompt size, and the estimator that decides whether a model is skipped

`runs/llm/chain.py: fits_context` is the guard that turned silent truncation into a
journaled `skipped_capability`. It compared `len(prompt) / 3.5` against `max_ctx - 1024`.
Against the tokenizers' own counts on the 40 lean prompts, with the shipped estimator
(`real` includes the provider's ~45-token system message and chat template, which the
estimate does not see):

| model | regime | n | est p50 | real p50 | real/est min | p50 | max | real/(chars/3.5) p50 | real max |
|---|---|---|---|---|---|---|---|---|---|
| granite4.2:3b | lean | 40 | 5671 | 5268 | 0.912 | 0.926 | 0.943 | 1.51 | 6122 |
| qwen3.5:4b-q4_K_M | lean | 40 | 5671 | 5673 | 0.954 | 0.990 | 1.008 | 1.61 | 6597 |

The old estimate is 1.45–1.66× too low on both tokenizers. Candidate estimators, measured
(`real / estimate`, system message and template subtracted; `est>=real` is the number of
prompts the guard would have sized correctly):

| estimator | granite real/est min · p50 · max | est ≥ real | qwen real/est min · p50 · max | est ≥ real |
|---|---|---|---|---|
| `chars / 3.5` (old) | 1.446 · 1.494 · 1.565 | 0/40 | 1.519 · 1.600 · 1.664 | 0/40 |
| `chars / 2.0` | 0.819 · 0.848 · 0.888 | 40/40 | 0.860 · 0.909 · 0.946 | 40/40 |
| pieces, digits in runs of ≤3 | 1.029 · 1.063 · 1.077 | 0/40 | 1.060 · 1.143 · 1.159 | 0/40 |
| pieces, digits in runs of ≤2 | 1.007 · 1.031 · 1.045 | 0/40 | 1.038 · 1.109 · 1.125 | 0/40 |
| **pieces, one per digit (shipped)** | **0.904 · 0.916 · 0.930** | **40/40** | **0.941 · 0.981 · 0.999** | **40/40** |

`chars / 2.0` is safe but blunt (it would over-count prose 1.75×, and `brief`'s context
packs go to the same guard). The shipped estimator is the **larger** of `chars / 3.5` and a
piece count (`[A-Za-z]+ | \d | [^\w\s] | _`): prose keeps its old figure; dense JSON gets
one token per word, per digit and per symbol, which is what a BPE tokenizer does to it.
qwen3.5 splits every digit, so the three-digit grouping Llama-3 uses under-counted it by
up to 16% and was dropped.

The lean prompt's size is pinned by a test
(`tests/test_signals/test_screener.py: test_a_five_candidate_batch_fits_local_small_with_margin`):
a production-shaped cycle — 107 pairs, 20 rich, 60 news items, five candidates naming three
pairs plus a `news_event` — must fit `capabilities.local_small.max_ctx` from the committed
`models.yaml` with the answer reserve and 1,024 tokens to spare, counted with the router's
estimator; and the full render of the same cycle must not.

## 2. Latency

| model | regime | n ok | p50 | p95 | >90s | >180s | gen tok/s | load s | prompt tok | out tok | loadavg p50 / p95 | cost/call |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| granite4.2:3b | lean | 40 | 12.0s | 20.9s | 0/40 | 0/40 | 62.7 | 0.0 | 5268 | 603 | 8.6 / 9.1 | free |
| qwen3.5:4b-q4_K_M | lean | 40 | 11.6s | 16.4s | 0/40 | 0/40 | 49.5 | 0.01 | 5673 | 394 | 7.6 / 8.6 | free |
| haiku | lean | 39 | 78.6s | 112.0s | 8/39 | 1/39 | — | — | — | 8487 | 8.4 / 14.9 | $0.0699 |
| haiku | full | 14 | 70.1s | 97.2s | 4/14 | 0/14 | — | — | — | 6823 | 5.6 / 7.9 | $0.1303 |

Both small models are GPU-resident and load-insensitive, as the 09-25 eval found at
loadavg 33 and 4; today's runs sat at loadavg 7.6–9.1 and the numbers are within a second
of the quiet-host figures. `load_duration` was 0 on every call after the first
(8.6 s cold on granite). Haiku on the lean prompt is 78.6 s p50 and breached the
configured `tasks.scan.deadline_s: 180` once in 39 (188.6 s at loadavg 14) — the cloud
model is the binding party for that deadline, exactly as the 09-25 document said. (Haiku's
"out tok" is the SDK's whole-session count, not the answer alone.)

## 3. Screening quality

Host verification (`screener.verify`) applied to every answer; "gray-zone items" are
kept items whose score lies in `signals.scanner.screen.gray_zone` and would trigger the
paid cloud re-run.

| model / regime | ok | schema ok | items | host-dropped | items citing an invented key | invented news hashes | keep | distinct scores | gray-zone items | batches needing a cloud re-run |
|---|---|---|---|---|---|---|---|---|---|---|
| granite4.2:3b lean | 40/40 | 40/40 | 197 | 1 (1%) | 0 (0%) | 0 | 90% | 61 | 13 (7%) | 11/40 |
| qwen3.5:4b-q4_K_M lean | 40/40 | 40/40 | 200 | 0 (0%) | 0 (0%) | 0 | 62% | 21 | 53 (26%) | 28/40 |
| haiku lean | 39/39 | 39/39 | 195 | 0 (0%) | 0 (0%) | 0 | 66% | 48 | 69 (35%) | 33/39 |
| haiku full | 14/14 | 14/14 | 70 | 0 (0%) | 0 (0%) | 0 | 71% | 36 | 26 (37%) | 11/14 |

The `high_30d` citation bug from 09-25 is gone (`dip_from_high` now cites it only where it
exists): across 397 local items and 265 Haiku items **no model invented a single feature
key or news hash**. Granite's one host-drop was a duplicate `signal_id`. Granite's scores
are the least often in the gray zone (7% of items, 11 of 40 batches escalate); Haiku's own
scores land there 35% of the time — the gray band is calibrated to Haiku's caution, and
would be worth re-measuring against granite's distribution once it has run for a month.

### Agreement on the decision that actually matters

Agreement is on the `screened` / `screened_out` decision `pipeline._apply_score` would
reach (detector 0.6 + screener 0.4 against `min_score 0.60`, `keep: false` always out),
per item, on batches both parties answered.

| A vs B (reference) | batches | items | agree | A screened, B not (wasted escalation) | A screened_out, B screened (missed signal) |
|---|---|---|---|---|---|
| haiku FULL vs haiku lean | 14 | 70 | 74% | 14 (20%) | 4 (6%) |
| **granite4.2:3b lean vs haiku lean** | 39 | 195 | **76%** | 43 (22%) | **3 (2%)** |
| granite4.2:3b lean vs haiku FULL | 14 | 70 | 79% | 11 (16%) | 4 (6%) |
| qwen3.5:4b-q4_K_M lean vs haiku lean | 39 | 195 | 68% | 29 (15%) | **33 (17%)** |
| qwen3.5:4b-q4_K_M lean vs haiku FULL | 14 | 70 | 77% | 4 (6%) | 12 (17%) |
| granite4.2:3b lean vs qwen3.5:4b-q4_K_M lean | 40 | 200 | 75% | 47 (24%) | 3 (2%) |

Reading the asymmetry: **"A screened, B not"** is a wasted escalation (a validation that
Haiku would not have started — bounded by `signals.validator.max_per_day` and the per-asset
cooldown); **"A screened_out, B screened"** is a missed signal, which nothing downstream
can recover. The owner's standing order is *"not fewer trades but better trades"*: the
error to minimise is the second column, and granite's is 2% against qwen's 17%. That, not
speed (they tie) or schema (both perfect), is what decides the model.

What each party would send to the validator, over the same 200 items:

| party | screened |
|---|---|
| detector alone (the status quo whenever the local model is skipped and Haiku fails) | 177 (88%) |
| granite4.2:3b, lean | 166 (83%) |
| haiku, full | 50 of 70 (71%) |
| haiku, lean | 123 of 195 (63%) |
| qwen3.5:4b, lean | 122 (61%) |

Granite screens **more** than today's Haiku-only path does (83% vs 63%) and only a little
less than no screener at all. The switch therefore raises the number of validations
started per day, up to the validator's cap; what it removes is the Haiku first pass on
every cycle (~$0.07 × every screened batch) and the 60–190 s it costs.

### Does slimming change Haiku's own answer?

On 14 batches it agreed with itself 74% of the time. The disagreement is one-sided: with
the full render Haiku kept 14 items the lean render sent out, and sent out 4 the lean
render kept. Seeing the other 101 pairs makes Haiku *more* permissive, not more selective —
the opposite of what "more context" is usually assumed to buy. Which answer is right cannot
be settled here: `signal_validations` holds 15 rows and none of them survived host
verification, so no screened signal has ever been graded. The lean render is the default
for the escalation because the re-run then judges the same evidence the head judged, at
$0.070 instead of $0.130; `FULL_PROMPT_ON_ESCALATION` is the one-line change if the first
month of stored validations says the lean re-run is throwing away valid signals.

## 4. Cost

| Haiku call | n | total | median | range | over `deadline_s: 180` |
|---|---|---|---|---|---|
| scan, lean | 39 | $2.8532 | $0.0699 | $0.0525–$0.1245 | 1/39 |
| scan, full | 14 | $1.8500 | $0.1303 | $0.1100–$0.1566 | 0/14 |
| **total** | **53** | **$4.7033** | | | |

Local calls cost nothing to meter; 80 of them ran in this measurement.

## 5. What changed in the code, and the test that fails without each change

| change | where | test |
|---|---|---|
| lean render: FEATURES → named pairs + core, NEWS → cited + relevant (cap 12), UNIVERSE gains `shown` | `runs/signals/screener.py: lean_features, render_prompt(lean=)` | `TestLeanPrompt.test_features_shrink_to_the_named_pairs_plus_core`, `test_news_keeps_what_is_cited_or_relevant_and_caps_the_rest` |
| the 5-candidate lean prompt fits `max_ctx` with margin; the full one does not | same | `test_a_five_candidate_batch_fits_local_small_with_margin` |
| CANDIDATES block and schema byte-identical across renders | same | `test_candidates_and_schema_are_byte_identical_between_renders` |
| `screen()` hands the chain the lean render by default and reports `prompt_mode` / `prompt_est_tokens`; the full render is behind `FULL_PROMPT_ON_ESCALATION`; both flags read `signals.scanner.screen.{lean_prompt,full_prompt_on_escalation}` once those keys exist | `screener.screen`, `screener.prompt_flags`, `pipeline.ScanReport.screen_prompt_*` | `test_screen_hands_the_chain_the_lean_render_and_says_so`, `test_the_full_render_is_one_flag_away`, `test_the_escalation_may_see_the_full_render_only_when_asked`, `test_flags_read_the_config_key_when_it_exists`, `test_a_scan_report_carries_the_prompt_mode` |
| template tells the model an unrendered pair was measured, not lost | `prompts/stages/scan.v2.md` (tier 1) | covered by the render tests (`"shown"` in the prompt) |
| estimator: max(chars/3.5, word/digit/symbol pieces) | `runs/llm/chain.py: estimated_tokens, _TOKEN_PIECES` | `tests/test_llm/test_token_estimate.py` (4 tests) |
| validator accepts a cited key that is any path in the pack, still refuses one that is not | `runs/signals/validator.py: citable_keys` | `test_a_path_that_exists_in_the_pack_is_a_legitimate_citation`, `test_a_path_the_pack_does_not_have_is_still_an_invention`, `test_citable_keys_are_the_features_plus_every_pack_path` |
| `models.local_small` → `granite4.2:3b`; `classify.why` no longer repeats the refuted "qwen answers 0-100" claim | `config/models.yaml` | `tests/test_llm/test_tier_matrix.py: TestTheLocalAliasIsTheMeasuredModel` (also pins `max_ctx == num_ctx`) |

## 6. Holdings watch, end to end

Run: `~/tmp/lt29/watch_demo.py granite4.2:3b` (WSL; scratch copies of the runtime's
journal, knowledge and both Freqtrade ledgers made with the sqlite backup API).

**A. The real watcher on the runtime's current positions.** `runs.watch.runner.run_once`
with `watch.task` as configured (`classify`) and as intended (`holdings_watch`):
`holdings=0 checked=0 model_calls=0 notes=['no open positions']`. Sleeve a's last trades
(BTC/USDT, ETH/USDT) closed 2026-09-23 21:58Z; sleeve b's on 2026-09-23 13:52Z. There has
been nothing to watch for six days.

**B. The two most recent real positions** (recorded `watch_events.facts_json`, sleeve a,
2026-09-23 21:56Z), through the real seam with granite at the head, the real thesis lookup
and the real news path (collect → nomic-embed-text dedupe → render → schema → citation
check):

| holding | thesis found | news window | answer | latency |
|---|---|---|---|---|
| ETH/USDT, at the recorded time | none | 2 collected → 2 shown (embedding) | `weakened` 0.8, cites both headline ids, both verify | 14.0 s (cold load) |
| ETH/USDT, now | none | 0 | `intact` 1.0 | 0.7 s |
| BTC/USDT, at the recorded time | none | 7 → 5 shown (embedding) | `intact` 0.0 | 1.2 s |
| BTC/USDT, now | none | 1 → 1 | `weakened` 0.8, cites the one headline ("Bitcoin holds $83,000 as ZEC drops 12%") | 1.0 s |

**C. The same BTC holding with a recorded-style thesis** ("trend leg above the 200-day;
invalidation: close below the 200-day or a corroborated hack / withdrawal halt at a top-3
venue"): quiet window → `intact`; a corroborated "Binance halts BTC withdrawals after
$400M hot-wallet exploit" headline beside a calm one → **`broken` 0.8, "A hack event is
corroborated, which matches the invalidation condition"** — but it **cited the calm
headline's id, not the hack's**. The citation verifies (it exists), so
`verify_citations` cannot catch a wrong-but-real citation. Schema compliance and hash
existence are solved; citation *accuracy* on a 3B model is not, and should be treated as
weak evidence by `runs/watch/policy.py` (which already requires 0.70 confidence for
`broken` and four hand-raises in an hour before waking anybody).

Two other observations from B: with **no thesis recorded**, the prompt falls back to
*"judge whether the headlines describe something that would change how a careful holder
feels"*, and the model answers `weakened` to a benign price headline — a watcher without a
thesis raises hands about noise. And the recorded rows said `news_shown: 0` at 21:56Z on
09-23, while the same query today returns 2 and 7 items inside that window: the rows had
not yet been asset-tagged when the watcher ran (classification is a later, batched job),
so the watcher saw an empty window that later filled in.

### What stops it from producing a useful, verified answer — and where each fix lives

| cause | evidence | fix | owner |
|---|---|---|---|
| **No thesis is ever stored.** The validator discarded 11 of 15 validations for `unknown feature_key: ['market_state.data_fresh', …]` — verbatim paths into the pack it was shown | `signal_validations.error`, 2026-09-23..29; ~$5.30 of Sonnet with nothing kept | `runs/signals/validator.py: citable_keys` — a citation may be any path that exists in the pack; an absent one is still refused | **this change** |
| 2 more validations failed `verdict=invalid requires suggested.direction == hold` | same table | the schema rule vs the prompt's "direction of concern"; `schemas/signals.py` + `prompts/stages/validate.v1.md` | tier 1/2, not this package |
| Proposals are all `abstain` / `{USDT: 1.0}` because `knowledge/state/latest.json` says `data_fresh: false, newest_data_age_min: 5320` (3.7 days) while the candles table is current to 2026-09-29T00:00Z | every proposal since 09-25; `market_state` block in each pack | the market-state job / freshness stamp — `ops/**`, `runs/market_state*` | tier 2 |
| `watch.task: classify` in `earn.yaml` — the watcher never runs `tasks.holdings_watch` (deadline 120 vs 90, `why:` text irrelevant) | `config/earn.yaml: watch.task` | set `watch.task: holdings_watch` | tier 2 (orchestrator) |
| `news_shown: 0` at cycle time because asset tags arrive with the later classify batch | B above | classify on ingest, or let `collect` fall back to a title match on the base symbol | `runs/ingest.py` / `runs/watch/headlines.py`, not this package |
| Wrong-but-existing citation on a 3B model | C above | keep `escalate.broken_min_confidence` / `hand_raises_to_escalate` as they are; a second opinion on `haiku` (the chain's escalation) before any flag reaches a human | policy, already in place |

## 7. Configuration set by this change

```yaml
models:
  local_small: { provider: ollama, id: "granite4.2:3b", tier: 1 }   # was llama3.1:8b

capabilities:
  local_small: { tools: false, skills: false, structured_output: true, max_ctx: 8192 }  # unchanged
providers:
  ollama:
    options: { temperature: 0, num_ctx: 8192 }   # unchanged; must equal max_ctx
```

`tasks.scan` is unchanged: `chain: [local_small, haiku]`, `escalation: haiku`,
`deadline_s: 180`, `max_turns: 4`. The lean render is what makes the first entry
serve at all. `granite4.2:3b` is already pulled on the host (`/api/tags`, 2.24 GB).

**Not changed, and why:** `runs/ingest.py: classify_news` still sends `pending[:25]`. The
09-25 measurement showed batch 5 is 17 accuracy points better for every model; the literal
is hard-coded in a tier-2 file this change may not edit. The orchestrator should make it a
config key (`ingest.classify_batch`, default 5) or change the literal.

## 8. Method, and what this cannot tell you

**Corpus.** 40 batches from real detector output on real data. The runtime's
`knowledge/earn.db` was copied (sqlite backup, WAL included) and back-filled from the
feathers in `data/binance` (1d from 2024-06, 4h from 2025-06, 1h from 2025-08; 1.39M
candles), then candles and news were deleted monotonically backwards to each cut-off so a
snapshot sees only what existed then. 32 cut-offs every 6 h across 2026-09-21..29 (real
news windows of 3–60 items; only the fast-test profile's 31 pairs priced, exactly as
production) and 8 historical cut-offs (98–107 pairs priced, quiet news). Batches are the
top `max_candidates_per_cycle` (5) by `Ctx.priority`, rendered through
`screener.render_prompt` itself — the shipped code, both regimes. 200 items in all; the
detector mix is `dip_from_high` 44%, `move` 41%, `breakout` 10%, `rsi_extreme` 5%,
`volume_spike` 1 item. No `news_event` candidate fired inside a cut-off, so the lean
render's "keep every cited item" rule is exercised by the unit test, not by this corpus.

**Execution.** Local: `chain.run_task(pin="local_small")` with the models file's own
`OllamaProvider` (`think: false`, `num_ctx 8192`, `max_ctx 8192`, so the context guard
was live; it refused nothing). Cloud: `chain.run_task(pin="haiku")` under
`envwrap.sh research`, with `tasks.scan.deadline_s` widened to 300 in memory so a slow
call could not truncate the reference set (breaches of the configured 180 are counted
instead). Every answer was checked with `schemas.signals.validate_screen` and
`screener.verify`.

**Limits.**

* Agreement with Haiku is a proxy for quality, not ground truth. `signal_validations`
  has 15 rows and none survived host verification, so nobody knows which screened signals
  were *right*. The first month of stored theses — which this change makes possible — is
  what turns this into an outcome measurement.
* The full-vs-lean comparison is 14 batches (70 items), budget-limited. It says whether
  Haiku's *decisions* move; it cannot say whether the 101 unrendered pairs would have
  mattered to a *rationale*.
* Granite's permissiveness (keep 90% vs Haiku's 66%) means more validations per day than
  today's Haiku-only screening, up to `signals.validator.max_per_day`. That cap and the
  per-asset cooldown are the real bound on the cost of this switch; the gray-zone re-run
  on Haiku (11 of 40 batches) is the other.
* Granite's rationales are weaker than its decisions: several read the threshold
  backwards ("21.5 below threshold 12.0") while scoring the item sensibly. The validator,
  not the screener, is where reasoning is paid for.
* One host, one GPU, one afternoon at loadavg 5–17. The 09-25 eval covered loadavg 33
  and 4 for the same two models with the same result.
* Holdings watch was exercised on two reconstructed positions and one hypothetical
  thesis. Its verified-citation rate is 100% and its citation *accuracy* is unmeasured
  beyond the one miss in §6-C.
* Not measured: `tasks.extract` and `tasks.flags` on the local model (no production
  traffic to build a corpus from), and `classify` (re-)measured — the 09-25 result
  (granite 79% exact, 1% false positives at batch 5) stands.

**Raw data**, outside the repo and outside any backup: `~/tmp/lt29/` in WSL —
`corpus.json`, `prompts.json`, `local.jsonl`, `cloud.jsonl`, `build_corpus.py`,
`render.py`, `run_local.py`, `run_cloud.py`, `calib.py`, `analyse.py`, `watch_demo.py`.
