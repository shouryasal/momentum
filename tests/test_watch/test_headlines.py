"""One story reported by three feeds must cost one call, and never more than one.

The embedding model is a cost optimisation, so the tests insist on two things: that it
works when the endpoint answers, and that its absence degrades to something worse but
never to nothing.
"""

from __future__ import annotations

from runs.watch import headlines as H
from tests.test_watch.conftest import NOW, seed_news


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeEmbed:
    """Returns a vector per title, keyed on a word so 'the same story' is controllable."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        if self.fail:
            return FakeResponse({"error": "no such model"}, status=404)
        vectors = []
        for title in json["input"]:
            lowered = title.lower()
            vectors.append([1.0, 0.0] if "hack" in lowered else [0.0, 1.0])
        return FakeResponse({"embeddings": vectors})


def collect(kdb, **kw):
    return H.collect(kdb, "BTC", window_min=180, limit=6, now=NOW, **kw)


def test_only_headlines_naming_the_asset_are_collected(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="a" * 64, title="Bitcoin rallies", assets=("BTC",))
    seed_news(kdb, url_hash="b" * 64, title="Solana outage", assets=("SOL",))
    assert [h.title for h in collect(kdb)] == ["Bitcoin rallies"]


def test_market_wide_stories_are_included_when_configured(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="c" * 64, title="CPI comes in hot", assets=(),
              event_class="macro")
    assert len(collect(kdb)) == 1
    assert collect(kdb, include_market_wide=False) == []


def test_stale_headlines_fall_out_of_the_window(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="d" * 64, title="Old news", minutes_ago=600)
    assert collect(kdb) == []


def test_the_archive_cluster_is_honoured_for_free(dbs):
    """Ingest already clustered these; the watcher does not pay to rediscover it."""
    _, kdb = dbs
    for i, src in enumerate(("CoinDesk", "TheBlock", "Decrypt")):
        seed_news(kdb, url_hash=str(i) * 64, title=f"Exchange hacked, per {src}",
                  source=src, cluster_id="cluster-hack")
    embed = FakeEmbed()
    out = H.cluster(collect(kdb), base_url="http://x", model="m", threshold=0.9,
                    token_threshold=0.5, limit=6, client=embed)
    assert len(out.items) == 1
    assert out.items[0].duplicates == 2
    assert out.method == "archive_only"
    assert embed.calls == 0          # one group: nothing to compare


def test_embeddings_merge_what_the_archive_missed(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="1" * 64, title="Major exchange hack drains hot wallet")
    seed_news(kdb, url_hash="2" * 64, title="Hack at exchange: funds moved overnight")
    seed_news(kdb, url_hash="3" * 64, title="Regulator approves a spot ETF")
    out = H.cluster(collect(kdb), base_url="http://x", model="m", threshold=0.9,
                    token_threshold=0.9, limit=6, client=FakeEmbed())
    assert out.method == "embedding"
    assert len(out.items) == 2
    assert out.considered == 3


def test_an_embedding_failure_degrades_to_words_not_to_silence(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="1" * 64, title="Bitcoin ETF inflows reach one billion")
    seed_news(kdb, url_hash="2" * 64, title="Bitcoin ETF inflows reach one billion today")
    seed_news(kdb, url_hash="3" * 64, title="Quantum threat to Bitcoin assessed")
    out = H.cluster(collect(kdb), base_url="http://x", model="m", threshold=0.9,
                    token_threshold=0.6, limit=6, client=FakeEmbed(fail=True))
    assert out.method == "tokens"
    assert out.embed_error and "404" in out.embed_error
    assert len(out.items) == 2


def test_no_endpoint_at_all_still_returns_headlines(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="1" * 64, title="Bitcoin dips")
    out = H.cluster(collect(kdb), base_url=None, model="m", threshold=0.9,
                    token_threshold=0.6, limit=6)
    assert len(out.items) == 1


def test_the_representative_is_the_best_source_not_the_first(dbs):
    """Corroborated beats uncorroborated; primary beats secondary."""
    _, kdb = dbs
    seed_news(kdb, url_hash="1" * 64, title="Exchange hacked", source="RandomBlog",
              source_class="secondary", corroborated=0, cluster_id="k", minutes_ago=10)
    seed_news(kdb, url_hash="2" * 64, title="Exchange hacked", source="Binance",
              source_class="primary", corroborated=1, cluster_id="k", minutes_ago=20)
    out = H.cluster(collect(kdb), base_url=None, model="m", threshold=0.9,
                    token_threshold=0.6, limit=6)
    assert out.items[0].source == "Binance"
    assert out.items[0].corroborated is True


def test_the_limit_is_a_hard_cap_on_the_prompt(dbs):
    _, kdb = dbs
    words = ["custody", "mining", "regulation", "derivatives", "payments", "privacy",
             "lightning", "tariffs", "elections", "banking", "energy", "stablecoins"]
    for i, word in enumerate(words):
        seed_news(kdb, url_hash=f"{i:064d}", title=f"Bitcoin and {word}")
    out = H.cluster(collect(kdb), base_url=None, model="m", threshold=0.99,
                    token_threshold=0.99, limit=4)
    assert len(out.items) == 4
    assert out.considered == 12
    assert out.clustered_away == 8


def test_the_rendered_headline_carries_a_short_id_and_no_url(dbs):
    _, kdb = dbs
    seed_news(kdb, url_hash="f" * 64, title="Bitcoin dips", corroborated=1)
    item = H.cluster(collect(kdb), base_url=None, model="m", threshold=0.9,
                     token_threshold=0.6, limit=4).items[0]
    payload = item.as_dict()
    assert payload["news_hash"] == "f" * 12
    assert "url" not in payload


def test_token_similarity_ignores_stop_words():
    assert H.token_similarity("Bitcoin ETF inflows surge",
                              "The Bitcoin ETF inflows surge on the day") > 0.7
    assert H.token_similarity("Bitcoin ETF inflows", "Solana outage halts network") == 0.0
