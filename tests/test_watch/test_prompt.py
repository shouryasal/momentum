"""The prompt budget, and the substitution bug that made the scan prompt unusable.

A prompt that exceeds ``providers.ollama.options.num_ctx`` is not slow, it is silently
wrong: Ollama keeps what fits and the model answers about input it never saw. That is
exactly how the signal screener spent weeks producing invented feature keys. So the size
of a watch prompt is asserted, not assumed, and the renderer is held to substituting each
placeholder exactly once.
"""

from __future__ import annotations

import json
import re

import pytest

from ops.config import REPO_ROOT, stage_prompt
from runs.signals.screener import fill
from runs.watch import headlines as H
from runs.watch import positions as pos
from runs.watch.invalidation import evaluate
from runs.watch.prompt import render
from runs.watch.thesis import Thesis
from tests.test_watch.conftest import (
    NOW,
    seed_book,
    seed_candles,
    seed_news,
    seed_proposal,
    seed_trade,
    seed_wallet,
)

#: The configured local context window. A watch prompt must fit inside it with room to
#: spare, because the answer has to fit too.
NUM_CTX = 8192


@pytest.fixture
def rendered(cfg, root, dbs):
    jdb, kdb = dbs
    seed_wallet(root, "a")
    seed_trade(root, "a", amount=0.02, open_rate=80_000.0, stop_loss=72_000.0)
    seed_book(kdb, "BTC/USDT", mid=84_000.0)
    seed_candles(kdb, "BTC/USDT", "1h", n=30, close=83_500.0)
    seed_proposal(jdb)
    for i in range(6):
        seed_news(kdb, url_hash=f"{i:064d}", title=f"Bitcoin story about topic {i*7}")
    h = pos.open_holdings(cfg, kdb=kdb, jdb=jdb, root=root, now=NOW)[0]
    told = Thesis(thesis="BTC trend is intact above the 200d.",
                  invalidation="A 4h close below 82,000 invalidates this thesis.",
                  source="proposal:r", author_model="claude-opus-5")
    news = H.cluster(H.collect(kdb, "BTC", window_min=180, limit=5, now=NOW),
                     base_url=None, model="m", threshold=0.9, token_threshold=0.99,
                     limit=5)
    return render(cfg, h, told, news, evaluate(told.invalidation, h.facts()), root=None)


def test_the_prompt_fits_the_local_context_window_with_room_to_spare(rendered):
    assert rendered.tokens < 1_500, rendered.text
    assert rendered.tokens < NUM_CTX // 4


def test_the_thesis_and_invalidation_appear_verbatim(rendered):
    """A paraphrase would quietly change the sentence the model is judging."""
    assert "BTC trend is intact above the 200d." in rendered.text
    assert "A 4h close below 82,000 invalidates this thesis." in rendered.text


def test_the_host_check_of_the_invalidation_is_shown(rendered):
    assert "checked, has NOT fired: price <= 82000" in rendered.text


def test_the_numbers_are_there_and_compact(rendered):
    assert '"pnl_pct"' in rendered.text
    assert '"dist_to_stop_pct"' in rendered.text
    assert ": " not in rendered.text.split("## Position")[1].split("```")[1]


def test_headlines_are_cited_by_a_short_id(rendered):
    block = rendered.text.split("## Fresh headlines")[1]
    assert '"id":"' in block
    assert "https://" not in block


def test_the_citable_names_are_advertised_to_the_verifier(rendered):
    assert "pnl_pct" in rendered.facts
    assert all(len(h) == 64 for h in rendered.news_hashes)


def test_no_header_comment_reaches_the_model(rendered):
    """Notes to the reader are not instructions to a model, and cost tokens either way."""
    assert "<!--" not in rendered.text
    assert "TIER 1" not in rendered.text


def test_every_placeholder_was_substituted(rendered):
    assert not re.search(r"\{\{[A-Z_]+\}\}", rendered.text)


# --------------------------------------------------------- the double-substitution bug


def test_a_placeholder_named_twice_is_rendered_once():
    """The scan-prompt bug, pinned: the header listed its own placeholders, and a chain of
    ``str.replace`` substituted the header too — doubling every data block."""
    template = ("<!-- placeholders: {{DATA}} -->\nbody\n{{DATA}}\n")
    out = fill(template, {"DATA": lambda: "PAYLOAD"})
    assert out.count("PAYLOAD") == 1


def test_a_data_block_containing_a_placeholder_is_not_re_substituted():
    """One pass never looks at what it just wrote."""
    out = fill("{{A}} and {{B}}", {"A": lambda: "{{B}}", "B": lambda: "second"})
    assert out == "{{B}} and second"


def test_an_unknown_placeholder_is_left_alone():
    assert fill("{{KNOWN}} {{UNKNOWN}}", {"KNOWN": lambda: "x"}) == "x {{UNKNOWN}}"


def test_a_block_nobody_asked_for_is_never_built():
    built = []

    def expensive():
        built.append(1)
        return "x"

    fill("no placeholders here", {"EXPENSIVE": expensive})
    assert built == []


def test_the_shipped_scan_prompts_do_not_name_their_placeholders_with_braces():
    """A regression guard on the prompt files themselves, not just on the renderer."""
    for name in ("scan.v1.md", "scan.v2.md"):
        path = REPO_ROOT / "prompts" / "stages" / name
        if not path.is_file():
            continue
        header = path.read_text(encoding="utf-8").split("-->", 1)[0]
        assert "{{" not in header, f"{name} header would be substituted"


def test_the_configured_watch_prompt_exists(cfg):
    assert (REPO_ROOT / stage_prompt(cfg, "watch")).is_file()


def test_the_watch_prompt_template_is_short(cfg):
    """Its body is paid for once per holding, every few minutes."""
    body = (REPO_ROOT / stage_prompt(cfg, "watch")).read_text(encoding="utf-8")
    assert len(body) < 3_000


def test_the_position_block_is_valid_json(rendered):
    block = rendered.text.split("## Position")[1].split("```json")[1].split("```")[0]
    payload = json.loads(block)
    assert payload["pair"] == "BTC/USDT"
    assert "not_computable" not in payload
