"""Ingest phases against httpx.MockTransport: idempotent candles with in-progress
correction, book fields, funding/OI upserts, news conditional-GET + dedupe,
two-source corroboration, macro blackout edges, phase isolation."""

import json
from datetime import timedelta

import httpx
import pytest

from ops.lib import flags as flagslib
from runs.ingest import Ingest

from .conftest import NOW

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
{items}</channel></rss>"""
ITEM = ("<item><title>{title}</title><link>{link}</link>"
        "<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>")


class Binance:
    """Scriptable fake for spot+futures endpoints."""

    def __init__(self, now=NOW):
        self.now_ms = int(now.timestamp() * 1000)
        self.kline_calls = 0
        self.feeds: dict[str, list[tuple[str, str]]] = {}
        self.feed_status: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        q = dict(request.url.params)
        if p == "/api/v3/klines":
            self.kline_calls += 1
            start = int(q["startTime"])
            step = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[q["interval"]]
            ks = []
            t = start
            while t <= self.now_ms and len(ks) < int(q["limit"]):
                close_t = t + step - 1
                ks.append([t, "100", "101", "99", "100.5", "10", close_t, "1000",
                           5, "5", "500", "0"])
                t += step
            return httpx.Response(200, json=ks)
        if p == "/api/v3/depth":
            return httpx.Response(200, json={
                "bids": [["100.0", "2.0"], ["99.8", "3.0"]],
                "asks": [["100.2", "1.5"], ["100.4", "2.5"]]})
        if p == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={
                "symbol": q["symbol"], "markPrice": "100.1",
                "lastFundingRate": "0.0001", "nextFundingTime": self.now_ms + 100})
        if p == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[
                {"symbol": q["symbol"], "fundingTime": self.now_ms - 8 * 3600 * 1000,
                 "fundingRate": "0.0001", "markPrice": "100"}])
        if p == "/fapi/v1/openInterest":
            return httpx.Response(200, json={"symbol": q["symbol"],
                                             "openInterest": "1234.5"})
        url = str(request.url)
        if url in self.feeds:
            status = self.feed_status.get(url, 200)
            if status == 304 and request.headers.get("If-None-Match"):
                return httpx.Response(304)
            items = "".join(ITEM.format(title=t, link=li) for t, li in self.feeds[url])
            return httpx.Response(200, content=RSS.format(items=items).encode(),
                                  headers={"etag": "v1"})
        return httpx.Response(404)


@pytest.fixture
def ing(cfg, dbs):
    root, jdb, kdb = dbs
    fake = Binance()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    (root / "config").mkdir(exist_ok=True)
    ingest = Ingest(cfg, kdb, client, now=NOW, root=root)
    return ingest, fake, kdb, root


def test_candles_idempotent_and_corrected(ing):
    ingest, fake, kdb, _ = ing
    ingest.refresh_candles()
    n1 = kdb.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    assert n1 > 0
    ingest.refresh_candles()  # rerun: same natural keys, no duplicates
    n2 = kdb.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    assert n2 == n1
    # BNB data-only symbol ingested too
    assert kdb.execute("SELECT COUNT(*) FROM candles WHERE pair='BNB/USDT'").fetchone()[0] > 0
    # the newest candle is in-progress (close_time >= now) and marked open
    row = kdb.execute("SELECT is_closed FROM candles WHERE pair='BTC/USDT' AND tf='1h'"
                      " ORDER BY open_time DESC LIMIT 1").fetchone()
    assert row["is_closed"] == 0


def test_books_fields(ing):
    ingest, _, kdb, _ = ing
    ingest.snapshot_books()
    r = kdb.execute("SELECT * FROM book_snapshots WHERE pair='BTC/USDT'").fetchone()
    assert r["best_bid"] == 100.0 and r["best_ask"] == 100.2
    assert r["mid"] == pytest.approx(100.1)
    assert r["spread_bps"] == pytest.approx(0.2 / 100.1 * 1e4)
    assert json.loads(r["levels_json"])["bids"][0] == [100.0, 2.0]


def test_funding_and_oi(ing):
    ingest, _, kdb, _ = ing
    ingest.refresh_funding()
    ingest.refresh_funding()  # idempotent upserts
    assert kdb.execute("SELECT COUNT(*) FROM funding_current").fetchone()[0] == 2
    assert kdb.execute("SELECT COUNT(*) FROM funding WHERE symbol='BTCUSDT'").fetchone()[0] == 1
    oi = kdb.execute("SELECT oi FROM open_interest WHERE symbol='ETHUSDT'").fetchone()
    assert oi["oi"] == 1234.5


def test_news_dedupe_and_conditional_get(ing, cfg):
    ingest, fake, kdb, _ = ing
    for f in cfg.news.whitelist:
        fake.feeds[f.url] = [("Bitcoin ETF sees record inflows", f"https://x/{f.name}/1")]
    ingest.pull_news()
    n1 = kdb.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
    assert n1 == len(cfg.news.whitelist)
    for f in cfg.news.whitelist:
        fake.feed_status[f.url] = 304  # etag honored -> no refetch, no dupes
    ingest.pull_news()
    assert kdb.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == n1


def test_corroboration_two_source_rule(ing, cfg):
    ingest, fake, kdb, _ = ing
    feeds = cfg.news.whitelist
    secondaries = [f for f in feeds if f.class_ == "secondary"][:2]
    primary = next(f for f in feeds if f.class_ == "primary")
    fake.feeds[secondaries[0].url] = [("Bitcoin ETF approved by regulator", "https://a/1")]
    fake.feeds[secondaries[1].url] = [("Bitcoin ETF approval confirmed", "https://b/1")]
    fake.feeds[primary.url] = [("Ethereum upgrade scheduled for December", "https://c/1")]
    ingest.pull_news()
    ingest.corroborate()
    rows = {r["url"]: r for r in kdb.execute("SELECT * FROM news_items")}
    assert rows["https://a/1"]["corroborated"] == 1  # two distinct secondary sources
    assert rows["https://b/1"]["corroborated"] == 1
    assert rows["https://c/1"]["corroborated"] == 1  # primary self-corroborates
    assert json.loads(rows["https://a/1"]["assets"]) == ["BTC"]
    assert rows["https://a/1"]["event_class"] == "etf"


def test_single_secondary_source_not_corroborated(ing, cfg):
    ingest, fake, kdb, _ = ing
    sec = next(f for f in cfg.news.whitelist if f.class_ == "secondary")
    fake.feeds[sec.url] = [("Bitcoin exchange hacked for millions", "https://solo/1")]
    ingest.pull_news()
    ingest.corroborate()
    r = kdb.execute("SELECT corroborated FROM news_items WHERE url='https://solo/1'").fetchone()
    assert r["corroborated"] == 0


def test_macro_blackout_edges(ing, cfg):
    ingest, _, kdb, root = ing
    flags_path = root / cfg.paths.flags_file
    (root / "config" / "macro_calendar.yaml").write_text(
        "events:\n  - { name: FOMC, at: '2026-09-22T08:30:00Z' }\n")
    # inside the +/-60min window
    ingest.update_macro_blackout()
    assert flagslib.entries_blocked(flags_path, "BTC/USDT", now=NOW)[0]
    # outside: ingest-set flag cleared, heartbeat keeps updated_at fresh
    later = NOW + timedelta(hours=3)
    ingest.now = later
    ingest.update_macro_blackout()
    blocked, why = flagslib.entries_blocked(flags_path, "BTC/USDT", now=later)
    assert not blocked, why


def test_phase_isolation(ing, monkeypatch):
    ingest, _, kdb, _ = ing

    def boom():
        raise RuntimeError("binance down")

    monkeypatch.setattr(ingest, "refresh_candles", boom)
    rc = ingest.run()
    assert rc == 1
    statuses = {r["phase"]: r["status"] for r in kdb.execute("SELECT * FROM ingest_runs")}
    assert statuses["candles"] == "error"
    assert statuses["books"] == "ok"  # later phases still ran


# ---------------------------------------------------------------- classifier

from ops.config import REPO_ROOT  # noqa: E402
from runs.decision_core import StageMeta, StageResult  # noqa: E402


@pytest.fixture
def classify_env(ing, monkeypatch):
    ingest, _, kdb, root = ing
    (root / "config" / "models.yaml").write_text(
        (REPO_ROOT / "config" / "models.yaml").read_text())
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return ingest, kdb, root


def _seed_item(kdb, title, h):
    kdb.execute(
        "INSERT INTO news_items(url_hash, source, source_class, title, url,"
        " fetched_at, classified_by) VALUES (?,?,?,?,?,?, 'rule')",
        (h, "CoinDesk", "secondary", title, f"https://n/{h}",
         NOW.strftime("%Y-%m-%dT%H:%M:%SZ")))
    kdb.commit()


def test_classifier_labels_and_corroborate_preserves(classify_env):
    ingest, kdb, _ = classify_env
    _seed_item(kdb, "Bitcoin exchange hacked", "kw1")           # keyword-labelable
    _seed_item(kdb, "Validators offline across the network", "ml1")  # rules can't
    calls = []

    def runner(prompt, **kw):
        calls.append((prompt, kw))
        return StageResult(True, json.dumps({"labels": [
            {"url_hash": "ml1", "event_class": "outage", "assets": ["ETH"]},
            {"url_hash": "bogus", "event_class": "hack", "assets": []},     # unknown
            {"url_hash": "kw1", "event_class": "nonsense", "assets": []},   # invalid
        ]}), StageMeta(subtype="success"))

    ingest.stage_runner = runner
    ingest.classify_news()
    assert len(calls) == 1
    prompt, kw = calls[0]
    assert "ml1" in prompt and "kw1" not in prompt  # only rule-unlabelable items sent
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["effort"] == "high" and kw["max_turns"] == 1
    r = kdb.execute("SELECT * FROM news_items WHERE url_hash='ml1'").fetchone()
    assert r["event_class"] == "outage" and r["classified_by"] == "model"
    assert json.loads(r["assets"]) == ["ETH"]
    kw1 = kdb.execute("SELECT event_class FROM news_items WHERE url_hash='kw1'").fetchone()
    assert kw1["event_class"] is None  # invalid model label discarded
    # corroborate keeps the model label instead of re-deriving from keywords
    ingest.corroborate()
    r = kdb.execute("SELECT * FROM news_items WHERE url_hash='ml1'").fetchone()
    assert r["event_class"] == "outage" and json.loads(r["assets"]) == ["ETH"]
    # already-model-classified items are not re-sent
    calls.clear()
    ingest.classify_news()
    assert not calls


def test_classifier_failure_rule_labels_stand(classify_env):
    ingest, kdb, _ = classify_env
    _seed_item(kdb, "Something unclassifiable happens", "u1")
    ingest.stage_runner = lambda prompt, **kw: StageResult(
        False, None, StageMeta(error="refused"))
    assert ingest._phase("classify", ingest.classify_news) is False  # isolated
    r = kdb.execute("SELECT event_class, classified_by FROM news_items"
                    " WHERE url_hash='u1'").fetchone()
    assert r["event_class"] is None and r["classified_by"] == "rule"
    status = kdb.execute("SELECT status FROM ingest_runs WHERE phase='classify'"
                         ).fetchone()
    assert status["status"] == "error"


def test_classifier_gated_on_config_and_credential(classify_env, monkeypatch):
    ingest, kdb, _ = classify_env
    _seed_item(kdb, "Something unclassifiable happens", "g1")
    calls = []
    ingest.stage_runner = lambda prompt, **kw: calls.append(1)
    ingest.cfg.news.classify.enabled = False
    ingest.classify_news()
    assert not calls
    ingest.cfg.news.classify.enabled = True
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    ingest.classify_news()  # no credential -> silent skip
    assert not calls


# ---------------------------------------------------------------- claim check

class TestClaimCheck:
    TS = "2026-09-22T06:00:00Z"

    def _news(self, kdb, title, h, assets='["BTC"]'):
        kdb.execute(
            "INSERT INTO news_items(url_hash, source, source_class, title, url,"
            " published_at, fetched_at, classified_by, assets)"
            " VALUES (?,?,?,?,?,?,?, 'rule', ?)",
            (h, "CoinDesk", "secondary", title, f"https://n/{h}", self.TS,
             NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), assets))
        kdb.commit()

    def _candles(self, kdb):
        # 3 daily candles before the item: one -8% day; cumulative -8%
        ts_ms = 1_790_056_800_000  # 2026-09-22T06:00Z
        day = 86_400_000
        rows = [(100.0, 100.0, 3), (100.0, 92.0, 2), (92.0, 92.0, 1)]
        kdb.executemany(
            "INSERT INTO candles(pair, tf, open_time, open, close, is_closed)"
            " VALUES ('BTC/USDT','1d',?,?,?,1)",
            [(ts_ms - n * day, o, c) for o, c, n in rows])
        kdb.commit()

    def test_realistic_claim_verified(self, ing):
        ingest, _, kdb, _ = ing
        self._candles(kdb)
        self._news(kdb, "Bitcoin drops 8% after exchange hack", "cv1")
        ingest.claimcheck()
        r = kdb.execute("SELECT claim_verified FROM news_items WHERE url_hash='cv1'"
                        ).fetchone()
        assert r["claim_verified"] == 1  # 8% claimed, 8% realized

    def test_impossible_claim_falsified(self, ing):
        ingest, _, kdb, _ = ing
        self._candles(kdb)
        self._news(kdb, "Bitcoin crashes 40% in bloodbath", "cv2")
        ingest.claimcheck()
        r = kdb.execute("SELECT claim_verified FROM news_items WHERE url_hash='cv2'"
                        ).fetchone()
        assert r["claim_verified"] == 0  # 40% claimed, 8% realized

    def test_no_claim_or_ambiguous_assets_left_null(self, ing):
        ingest, _, kdb, _ = ing
        self._candles(kdb)
        self._news(kdb, "Bitcoin rallies on ETF news", "cv3")           # no %
        self._news(kdb, "Markets fall 9% broadly", "cv4",
                   assets='["BTC", "ETH"]')                             # 2 assets
        self._news(kdb, "Token up 8%", "cv5", assets="[]")              # no asset
        ingest.claimcheck()
        for h in ("cv3", "cv4", "cv5"):
            r = kdb.execute("SELECT claim_verified FROM news_items WHERE url_hash=?",
                            (h,)).fetchone()
            assert r["claim_verified"] is None, h
