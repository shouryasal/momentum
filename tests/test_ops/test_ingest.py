"""Ingest phases against httpx.MockTransport: idempotent candles with in-progress
correction, book fields, funding/OI upserts, news conditional-GET + dedupe,
two-source corroboration, macro blackout edges, phase isolation."""

import json
from datetime import timedelta

import httpx
import pytest

from ops.lib import flags as flagslib
from runs.ingest import Ingest, sym

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
