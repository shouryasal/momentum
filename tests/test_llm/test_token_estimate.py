"""The context guard's estimator: dense JSON is counted as dense JSON.

``fits_context`` exists to keep a prompt out of a window it would be silently truncated
in. Its estimate was ``chars / 3.5`` — right for prose, and 1.55-1.91x too LOW for the
scan prompt, which is mostly ``"BTC/USDT.rsi_4h":52.1`` (measured against the local
tokenizers, docs/design/local-model-choice.md §1). An optimistic guard is worse than none:
it says "~15,861 tokens" about a 28,000-token prompt and invites a ``num_ctx`` that still
truncates. The estimator is now the larger of the prose floor and a symbol-aware piece
count, and these tests pin both halves.
"""

from __future__ import annotations

import json

from runs.llm.chain import CHARS_PER_TOKEN, OUTPUT_RESERVE_TOKENS, estimated_tokens, fits_context

FEATURE_ROW = '"BTC/USDT.rsi_4h":52.1234,"BTC/USDT.ret_24h":-3.30573,"BTC/USDT.vol_z_1h":null,'


def test_prose_is_still_governed_by_the_characters_floor():
    prose = "the quick brown fox jumps over the lazy dog " * 200
    assert estimated_tokens(prose) == int(len(prose) / CHARS_PER_TOKEN) + 1


def test_dense_json_counts_every_symbol_and_digit():
    """The scan FEATURES block: ~2 characters per real token, not 3.5. Measured 2026-09-29
    on 40 real lean prompts: granite4.2:3b counted 1.45-1.57x and qwen3.5:4b 1.52-1.66x
    the chars/3.5 figure; this estimator stays at or just above both (real/est 0.90-0.999)."""
    block = "{" + FEATURE_ROW * 300 + "}"
    est = estimated_tokens(block)
    floor = int(len(block) / CHARS_PER_TOKEN) + 1
    assert est >= 1.5 * floor, (est, floor)
    assert est <= len(block) / 1.2, "not absurdly pessimistic either"


def test_the_estimate_is_never_below_the_prose_floor():
    for text in ("", "x", "word " * 50, json.dumps({"a": list(range(500))}), FEATURE_ROW):
        assert estimated_tokens(text) >= int(len(text) / CHARS_PER_TOKEN) + 1


def test_a_features_block_that_chars_over_3_5_would_have_passed_is_now_skipped():
    """The trap the old estimate set: ~25,000 real tokens read as ~15,900 and 'fit' a
    16,384 window. The measured ratio for this payload is ~1.8x."""
    block = "{" + FEATURE_ROW * 700 + "}"          # ~56k chars, like the prod prompt
    old_estimate = int(len(block) / CHARS_PER_TOKEN) + 1
    assert old_estimate + OUTPUT_RESERVE_TOKENS <= 32768   # the old guard would let it in
    fits, est = fits_context(block, 32768)
    assert est > old_estimate * 1.5
    # and 8192 is out of the question for either estimate
    assert not fits_context(block, 8192)[0]
