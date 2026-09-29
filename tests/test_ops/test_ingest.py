"""Ingest phases against httpx.MockTransport: idempotent candles with in-progress
correction, book fields, funding/OI upserts, news conditional-GET + dedupe,
two-source corroboration, macro blackout edges, phase isolation."""

import json
from datetime import timedelta

import httpx
import pytest

from ops.lib import flags as flagslib
from runs.ingest import FUT, TRADING_PHASES, Ingest

from .conftest import NOW

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
{items}</channel></rss>"""
ITEM = ("<item><title>{title}</title><link>{link}</link>"
        "<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>")


class Binance:
    """Scriptable fake for spot+futures endpoints."""

    #: Symbols the fake futures venue lists a tradeable perpetual for. ``None`` means
    #: "every symbol asked for", which is the old, pre-discovery world.
    perps: set[str] | None

    def __init__(self, now=NOW):
        self.now_ms = int(now.timestamp() * 1000)
        self.kline_calls = 0
        self.feeds: dict[str, list[tuple[str, str]]] = {}
        self.feed_status: dict[str, int] = {}
        self.perps = None
        self.exchange_info_calls = 0
        #: symbols premiumIndex/fundingRate/openInterest were actually asked about
        self.funding_asked: list[str] = []
        #: symbol -> status to answer with instead of 200 (e.g. a transient 500)
        self.fut_status: dict[str, int] = {}
        #: when set, /fapi/v1/exchangeInfo itself fails
        self.exchange_info_status: int | None = None

    def _fut_symbol(self, q: dict) -> httpx.Response | None:
        """400 for a symbol this venue has no perpetual for — exactly what Binance does."""
        s = q.get("symbol", "")
        self.funding_asked.append(s)
        if self.perps is not None and s not in self.perps:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        forced = self.fut_status.get(s)
        if forced:
            return httpx.Response(forced, json={"code": -1001, "msg": "forced"})
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        q = dict(request.url.params)
        if p == "/fapi/v1/exchangeInfo":
            self.exchange_info_calls += 1
            if self.exchange_info_status:
                return httpx.Response(self.exchange_info_status, json={"msg": "down"})
            listed = sorted(self.perps) if self.perps is not None else []
            return httpx.Response(200, json={"symbols": [
                {"symbol": s, "status": "TRADING", "contractType": "PERPETUAL"}
                for s in listed]})
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
            bad = self._fut_symbol(q)
            return bad or httpx.Response(200, json={
                "symbol": q["symbol"], "markPrice": "100.1",
                "lastFundingRate": "0.0001", "nextFundingTime": self.now_ms + 100})
        if p == "/fapi/v1/fundingRate":
            bad = self._fut_symbol(q)
            return bad or httpx.Response(200, json=[
                {"symbol": q["symbol"], "fundingTime": self.now_ms - 8 * 3600 * 1000,
                 "fundingRate": "0.0001", "markPrice": "100"}])
        if p == "/fapi/v1/openInterest":
            bad = self._fut_symbol(q)
            return bad or httpx.Response(200, json={"symbol": q["symbol"],
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


def test_funding_and_oi(ing, cfg):
    ingest, _, kdb, _ = ing
    ingest.refresh_funding()
    ingest.refresh_funding()  # idempotent upserts
    # One row per tradeable pair: the universe is resolved from a snapshot now, so the
    # count tracks it rather than a hardcoded 2.
    assert kdb.execute("SELECT COUNT(*) FROM funding_current").fetchone()[0] == len(
        cfg.universe.pairs
    )
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


# ------------------------------------------------- 2026-09-24: PEPE killed the data chain
#
# PEPE/USDT has no PERPETUAL contract, so fapi/v1/premiumIndex?symbol=PEPEUSDT answers 400.
# Ingest asked anyway, the funding phase raised on the 16th of 31 pairs, and `run()` returned
# non-zero because *any* failed phase failed the job. That killed the ingest cron: candles
# froze at 06:00, freshness froze with them, healthcheck latched `data_stale` with
# `expires_at: null`, and the gate refused 655 consecutive entries over 14 hours.
#
# Three separate defects, one per test group below: a missing perp treated as an error, a
# non-trading phase able to fail the job, and a latch with no way back.


class TestMissingPerpetualIsAKnownCase:
    def test_a_pair_with_no_perp_is_recorded_not_an_error(self, ing, cfg):
        ingest, fake, kdb, _ = ing
        fake.perps = {sym for sym in (p.replace("/", "") for p in cfg.universe.pairs)
                      if sym != "PEPEUSDT"}
        assert ingest._phase("funding", ingest.refresh_funding) is True
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert "no perp: PEPE/USDT" in row["detail"]
        # `ok`, not `degraded`: nothing failed, there is simply no contract to read. Every
        # non-`ok` status is folded into "this phase is failing" by ops/autonomy.py, and a
        # permanent failing badge for a permanent fact is how the real outage stayed hidden.
        assert row["status"] == "ok"
        # and PEPE was never asked about — the 400 is avoided, not caught
        assert "PEPEUSDT" not in fake.funding_asked
        # every other pair still got its funding, history and OI
        assert kdb.execute("SELECT COUNT(*) FROM funding_current").fetchone()[0] == len(
            cfg.universe.pairs) - 1
        assert kdb.execute(
            "SELECT COUNT(*) FROM funding_current WHERE symbol='PEPEUSDT'").fetchone()[0] == 0
        assert kdb.execute(
            "SELECT COUNT(*) FROM funding_current WHERE symbol='BTCUSDT'").fetchone()[0] == 1

    def test_coverage_is_discovered_once_a_day_not_once_a_cycle(self, ing, cfg):
        ingest, fake, _, _ = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        ingest.refresh_funding()
        ingest.refresh_funding()
        assert fake.exchange_info_calls == 1, "the perp map must be cached between cycles"

    def test_one_transient_symbol_failure_does_not_lose_the_others(self, ing, cfg):
        """A symbol that 500s after discovery is skipped and counted, never fatal."""
        ingest, fake, kdb, _ = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        fake.fut_status["ETHUSDT"] = 500
        assert ingest._phase("funding", ingest.refresh_funding) is True
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert row["status"] == "degraded" and "ETHUSDT" in row["detail"]
        assert kdb.execute("SELECT COUNT(*) FROM funding_current").fetchone()[0] == len(
            cfg.universe.pairs) - 1

    def test_a_total_futures_outage_is_still_a_phase_failure(self, ing, cfg):
        """Degrading is for partial loss. Every symbol failing is a real outage."""
        ingest, fake, kdb, _ = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        for s in fake.perps:
            fake.fut_status[s] = 500
        assert ingest._phase("funding", ingest.refresh_funding) is False
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert row["status"] == "error"

    def test_discovery_being_down_does_not_stop_funding_collection(self, ing, cfg):
        """No map and no endpoint: try everything, report it. Never "no pair has a perp"."""
        ingest, fake, kdb, _ = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        fake.exchange_info_status = 503
        assert ingest._phase("funding", ingest.refresh_funding) is True
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert "perp map unavailable" in row["detail"]
        assert row["status"] == "degraded"     # this one IS a fault: we could not find out
        assert kdb.execute("SELECT COUNT(*) FROM funding_current").fetchone()[0] == len(
            cfg.universe.pairs)

    def test_the_rate_budget_guard_is_never_forgiven_as_a_symbol_failure(self, ing, cfg,
                                                                        monkeypatch):
        """Per-symbol tolerance must not swallow the IP weight breaker.

        Skipping a symbol that 400s is right. "Skipping" a rate-limit breach and then asking
        for the next thirty symbols anyway is how a soft guard turns into a ban — so the guard
        is its own exception type and aborts the phase.
        """
        from runs.ingest import RateBudgetExceeded

        ingest, fake, kdb, _ = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        calls = []
        real = ingest._get

        def guarded(url, params):
            calls.append(url)
            if len(calls) > 4:
                raise RateBudgetExceeded("rate budget guard: used-weight-1m=5000")
            return real(url, params)

        monkeypatch.setattr(ingest, "_get", guarded)
        assert ingest._phase("funding", ingest.refresh_funding) is False
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert row["status"] == "error" and "rate budget guard" in row["detail"]
        assert len(calls) == 5, "the phase kept asking after the weight guard tripped"

    def test_a_weight_breach_during_discovery_stops_on_the_spot(self, ing, cfg,
                                                                monkeypatch):
        """It must abort at the breach, not be relabelled "discovery unavailable".

        Without the re-raise the breach becomes ``perp map unavailable``, which reports every
        pair as covered — so the phase goes on to make another request before a downstream
        guard stops it. One extra request is small; the contract is not, because
        ``perp_symbols`` is shared and a caller with no per-symbol guard would walk the whole
        book. Hence the exact call count.
        """
        from runs.ingest import RateBudgetExceeded

        ingest, fake, kdb, _ = ing
        calls = []

        def guarded(url, params):
            calls.append(url)
            raise RateBudgetExceeded("rate budget guard: used-weight-1m=5000")

        monkeypatch.setattr(ingest, "_get", guarded)
        assert ingest._phase("funding", ingest.refresh_funding) is False
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert "rate budget guard" in row["detail"]
        assert calls == [f"{FUT}/fapi/v1/exchangeInfo"], \
            "the phase made another request after the weight guard tripped"

    def test_a_known_gap_and_a_real_fault_are_told_apart(self, ing, cfg):
        """Both facts reach the row, but only the fault sets the status.

        ``ops/autonomy.py`` reads any non-``ok`` status as "this phase is failing". If a
        permanent no-perp gap set that, the ops panel would show ingest as permanently broken
        and everyone would learn to ignore it — which is precisely how a real 14-hour outage
        went unnoticed. So the gap is reported, and the fault is what raises the flag.
        """
        ingest, fake, kdb, _ = ing
        fake.perps = {sym for sym in (p.replace("/", "") for p in cfg.universe.pairs)
                      if sym != "PEPEUSDT"}
        fake.fut_status["ETHUSDT"] = 500
        ingest._phase("funding", ingest.refresh_funding)
        row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='funding'").fetchone()
        assert row["status"] == "degraded"
        assert "no perp: PEPE/USDT" in row["detail"] and "ETHUSDT" in row["detail"]


class TestOnlyTradingPhasesFailTheJob:
    def test_a_funding_outage_does_not_stop_the_ingest_job(self, ing, cfg, monkeypatch):
        """The exact 2026-09-24 failure: funding dies, candles are fine, job must succeed."""
        ingest, fake, kdb, _ = ing
        monkeypatch.setattr(ingest, "refresh_funding",
                            lambda: (_ for _ in ()).throw(RuntimeError("400 PEPEUSDT")))
        assert ingest.run() == 0, "a funding outage must not fail the job"
        statuses = {r["phase"]: r["status"] for r in kdb.execute("SELECT * FROM ingest_runs")}
        assert statuses["funding"] == "error"     # recorded, visible, not hidden
        assert statuses["candles"] == "ok"
        # the thing that actually mattered: candles were written
        assert kdb.execute("SELECT COUNT(*) FROM candles").fetchone()[0] > 0

    @pytest.mark.parametrize("phase", ["news", "classify", "corroborate", "claimcheck",
                                       "macro"])
    def test_no_non_trading_phase_can_fail_the_job(self, ing, phase, monkeypatch):
        ingest, *_ = ing
        attr = {"news": "pull_news", "classify": "classify_news",
                "corroborate": "corroborate", "claimcheck": "claimcheck",
                "macro": "update_macro_blackout"}[phase]
        monkeypatch.setattr(ingest, attr,
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        assert ingest.run() == 0

    @pytest.mark.parametrize("phase,attr", [("candles", "refresh_candles"),
                                            ("books", "snapshot_books")])
    def test_a_trading_phase_failing_still_fails_the_job(self, ing, phase, attr,
                                                         monkeypatch):
        """The half of the guarantee that must not be weakened: no prices, non-zero exit."""
        ingest, *_ = ing
        monkeypatch.setattr(ingest, attr,
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        assert ingest.run() == 1
        assert phase in TRADING_PHASES

    def test_funding_freshness_is_advisory_not_blocking(self, ing, cfg):
        """A funding stamp must not be able to stop an entry."""
        from ops.lib import freshness as freshlib

        ingest, fake, _, root = ing
        fake.perps = {p.replace("/", "") for p in cfg.universe.pairs}
        ingest._phase("funding", ingest.refresh_funding)
        data = freshlib.read(root / freshlib.FRESHNESS_REL)
        assert freshlib.SOURCE_FUNDING in data[freshlib.ADVISORY_KEY]
        assert freshlib.SOURCE_FUNDING not in data["sources"]


class TestIngestRecoversTheStalenessFlag:
    """The flag could only ever be cleared by healthcheck, and healthcheck had stopped.

    Ingest is the process that *restores* freshness, so it re-evaluates the latch too: two
    independent ways back instead of one single point of failure.
    """

    def _latch(self, root, cfg, *, set_by="healthcheck", now=NOW):
        flagslib.set_flag(root / cfg.paths.flags_file, "data_stale",
                          severity="block_entries", reason="data age 999 min",
                          set_by=set_by, expires_at=None, now=now)
        assert flagslib.entries_blocked(root / cfg.paths.flags_file, "BTC/USDT",
                                       now=now)[0]

    def test_ingest_clears_the_latch_once_prices_are_fresh(self, ing, cfg):
        ingest, _, _, root = ing
        self._latch(root, cfg)
        assert ingest.run() == 0
        blocked, why = flagslib.entries_blocked(root / cfg.paths.flags_file, "BTC/USDT",
                                                now=NOW)
        assert not blocked, f"the gate is still blocked on {why!r} with fresh candles"

    def test_the_latch_stands_while_prices_really_are_stale(self, ing, cfg):
        """Fail-closed is the point. Only recovery was broken, not the block."""
        ingest, _, kdb, root = ing
        ingest.run()                       # write fresh candles
        self._latch(root, cfg)
        ingest.now = NOW + timedelta(hours=9)   # ...then let them go stale
        note = ingest.reconcile_staleness_flag()
        assert note.status == "degraded" and note.detail.startswith("data_stale stands")
        assert flagslib.entries_blocked(root / cfg.paths.flags_file, "BTC/USDT",
                                       now=ingest.now)[0]

    def test_ingest_never_clears_a_flag_a_human_set(self, ing, cfg):
        ingest, _, _, root = ing
        self._latch(root, cfg, set_by="human")
        ingest.run()
        assert flagslib.entries_blocked(root / cfg.paths.flags_file, "BTC/USDT",
                                       now=NOW)[0]

    def test_ingest_never_sets_the_flag_itself(self, ing, cfg):
        """Setting (and alerting) stays healthcheck's job; ingest only ever lifts."""
        ingest, _, _, root = ing
        flags_path = root / cfg.paths.flags_file
        ingest.now = NOW + timedelta(days=3)     # nothing fresh anywhere
        ingest.reconcile_staleness_flag()
        try:
            assert "data_stale" not in flagslib.active_flags(flags_path, ingest.now)
        except flagslib.FlagsError:
            pass  # no flags file at all is equally fine: nothing was set

    def test_a_stale_news_feed_does_not_keep_the_gate_shut(self, ing, cfg):
        """News in the staleness clock was the crisis audit's finding. Pin it end to end."""
        from ops.lib import freshness as freshlib

        ingest, _, kdb, root = ing
        ingest.run()
        # a news item from a fortnight ago is the newest one there is
        kdb.execute("INSERT INTO news_items(url_hash, source, source_class, title, url,"
                    " fetched_at, classified_by) VALUES ('old','X','secondary','t',"
                    " 'https://x/old', ?, 'rule')",
                    ((NOW - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        kdb.commit()
        ingest.write_freshness()
        self._latch(root, cfg)
        note = ingest.reconcile_staleness_flag()
        assert note.status == "ok" and "cleared" in note.detail
        blocked, why = flagslib.entries_blocked(root / cfg.paths.flags_file, "BTC/USDT",
                                                now=NOW)
        assert not blocked, f"a two-week-old news item is still blocking entries ({why!r})"
        # ...and it is reported rather than ignored
        assert freshlib.SOURCE_NEWS in freshlib.degraded(
            30.0, path=root / freshlib.FRESHNESS_REL, now=NOW)


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


class TestTheTrendWriterRunsEveryIngestCycle:
    """The ensemble gate fails CLOSED on a missing/stale trend.json — so the writer must be
    a recurring phase, not a function with no caller (which is how it was handed over on
    2026-09-29; deployed alone it would have stopped every BTC/ETH entry on both sleeves)."""

    @staticmethod
    def _payload(root, weights):
        assets = {a: {"asset": a, "weight": w, "status": "ok" if w is not None else "warmup",
                      "asof_open_utc": "2026-09-28T00:00:00Z",
                      "asof_close_utc": "2026-09-29T00:00:00Z", "close": 1.0, "bars": 300,
                      "members_on": 15 if w else 0, "members": {}, "detail": ""}
                  for a, w in weights.items()}
        return {"version": 1, "assets": assets}

    def test_the_phase_is_in_the_cycle_right_after_candles(self, ing):
        import inspect

        from runs.ingest import TRADING_PHASES, Ingest

        src = inspect.getsource(Ingest.run)
        assert src.index('("candles"') < src.index('("trend"') < src.index('("books"')
        assert "trend" not in TRADING_PHASES, "a dead trend writer must never stop the feeds"

    def test_a_full_weight_set_is_ok_and_names_the_bar(self, ing, monkeypatch):
        ingest, _, kdb, root = ing
        from runs.features import trend
        (root / "knowledge" / "state").mkdir(parents=True, exist_ok=True)
        target = root / "knowledge" / "state" / "trend.json"

        def fake_write(cfg, kdb_, root_, *, now=None):
            target.write_text(json.dumps(self._payload(root_, {"BTC": 1.0, "ETH": 0.5})))
            return target
        monkeypatch.setattr(trend, "write_state", fake_write)
        note = ingest.refresh_trend()
        assert note.status == "ok"
        assert "BTC=1.00" in note.detail and "ETH=0.50" in note.detail
        assert "2026-09-29T00:00:00Z" in note.detail

    def test_a_missing_weight_is_degraded_and_names_the_asset(self, ing, monkeypatch):
        ingest, _, kdb, root = ing
        from runs.features import trend
        (root / "knowledge" / "state").mkdir(parents=True, exist_ok=True)
        target = root / "knowledge" / "state" / "trend.json"

        def fake_write(cfg, kdb_, root_, *, now=None):
            target.write_text(json.dumps(self._payload(root_, {"BTC": 1.0, "ETH": None})))
            return target
        monkeypatch.setattr(trend, "write_state", fake_write)
        note = ingest.refresh_trend()
        assert note.status == "degraded"
        assert "ETH:warmup" in note.detail and "BTC" not in note.detail.split("no weight for")[1]


class TestKeywordRulesMatchWordsNotSubstrings:
    """The rule labels were 51% correct on 88 real headlines, and one keyword did it.

    ``lawsuit: [sec, …]`` was matched as a SUBSTRING of a lowercased title, so "seconds",
    "secretary", "Security" and "securities" all became a `lawsuit` event. 41 of the rule's
    43 errors were that. It matters beyond tidiness for two reasons: `lawsuit` is one of the
    `news_event` fast-path classes, and `tasks.classify.on_all_failed: rule` makes these
    labels the FLOOR the classify task degrades to — so every silent classify failure handed
    the gate a label that was wrong more often than right.
    """

    @staticmethod
    def _event(ingest, title):
        from runs.ingest import Ingest
        return next((ev for ev, kws in ingest.event_keywords.items()
                     if Ingest._kw_hit(title.lower(), kws)), None)

    @pytest.mark.parametrize("title", [
        "Report arrives within seconds of the close",
        "Treasury secretary comments on digital assets",
        "Exchange adds Security features to custody",
        "Firm files for securities registration",
    ])
    def test_sec_inside_another_word_is_not_a_lawsuit(self, classify_env, title):
        ingest, _, _ = classify_env
        assert self._event(ingest, title) != "lawsuit", title

    @pytest.mark.parametrize("title,expected", [
        ("SEC charges exchange over unregistered offering", "lawsuit"),
        ("SEC. filing names three tokens", "lawsuit"),
        # Stems must survive their inflections — this is why a bare \b…\b was not the fix.
        ("Major exchange hacked for $40M", "hack"),
        ("Hacking group drains a bridge", "hack"),
        ("Token delisted from two venues", "delist"),
        ("Stablecoin depeg spreads to lending markets", "depeg"),
        ("Withdrawals halted after a node outage", "outage"),
        ("Fed signals a rate cut in December", "macro"),      # multi-word keyword
    ])
    def test_a_real_keyword_still_matches(self, classify_env, title, expected):
        ingest, _, _ = classify_env
        assert self._event(ingest, title) == expected, title

    def test_settlement_is_a_lawsuit_because_the_config_says_so_not_the_matcher(
            self, classify_env):
        """The word-boundary matcher and the keyword list divide the work, on purpose.

        ``settle`` + "ment" is not an inflection, so the matcher alone would NOT label
        "reaches settlement with the regulator" — a genuine lawsuit signal. On 2026-09-29
        ``settlement`` was added to ``config/earn.yaml: news.event_keywords.lawsuit`` (a
        blessed tier-2 change, made deliberately), so the label comes back — and so does the
        known cost: "instant settlement rails", a payments headline, is a false positive
        again. That trade-off belongs in the config, where an operator can see and reverse
        it, not hidden inside a regex. This test pins the division: the matcher stays
        boundary-strict; the list decides which stems are worth a false positive.
        """
        ingest, _, _ = classify_env
        assert "settlement" in ingest.event_keywords["lawsuit"], \
            "config/earn.yaml no longer lists `settlement`; update this test's expectation"
        assert self._event(ingest, "Firm reaches settlement with regulator") == "lawsuit"
        assert self._event(ingest, "Instant settlement rails go live") == "lawsuit"  # accepted cost
        assert self._event(ingest, "Firm settles with regulator") == "lawsuit"
        # the matcher itself still refuses non-inflection suffixes for stems NOT in the list
        from runs.ingest import Ingest
        assert not Ingest._kw_hit("firm reaches settlement", ["settle"])


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
    # `low`, not `high`: putting one label from a fixed set on a headline does not get
    # better with more thinking, and an invalid label is discarded and re-derived from the
    # keyword rules anyway (config/models.yaml: tasks.classify.why).
    # 2 turns, not 1: one to decide the label and one for the SDK to emit the structured
    # answer. A cap of 1 does not make it stricter, it makes every cloud call fail.
    assert kw["effort"] == "low" and kw["max_turns"] == 2
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


# ---------------------------------------------------------------- config + pipeline (P4)


def test_keyword_tables_come_from_config(ing):
    ingest, _, _, _ = ing
    assert ingest.event_keywords == dict(ingest.cfg.news.event_keywords)
    assert ingest.asset_keywords == dict(ingest.cfg.news.asset_keywords)
    ingest.cfg.news.event_keywords = {"custom": ["widget"]}
    assert ingest.event_keywords == {"custom": ["widget"]}


def test_timeframes_come_from_config(ing):
    ingest, fake, kdb, _ = ing
    ingest.cfg.ingest.timeframes = ["1h"]
    ingest.refresh_candles()
    tfs = {r[0] for r in kdb.execute("SELECT DISTINCT tf FROM candles")}
    assert tfs == {"1h"}


def test_freshness_file_is_written_after_every_phase(ing, cfg):
    ingest, _, _, root = ing
    ingest._phase("candles", ingest.refresh_candles)
    p = root / "knowledge" / "state" / "freshness.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["version"] == 1 and "candles_1h" in data["sources"]


def test_freshness_failure_never_fails_a_phase(ing, monkeypatch):
    ingest, _, _, _ = ing
    monkeypatch.setattr(ingest, "freshness_sources",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert ingest._phase("books", ingest.snapshot_books) is True


def test_classify_prompt_is_the_tier1_file(classify_env):
    ingest, kdb, _ = classify_env
    _seed_item(kdb, "Validators offline across the network", "p1")
    calls = []
    ingest.stage_runner = lambda prompt, **kw: (
        calls.append(prompt) or StageResult(True, json.dumps({"labels": []}),
                                            StageMeta(subtype="success")))
    ingest.classify_news()
    assert calls and "Label each crypto news headline" in calls[0]
    assert '"outage"' in calls[0]           # the configured event classes
    assert "p1" in calls[0]


def test_classify_routes_through_the_chain_router_when_no_stage_runner(classify_env):
    """With no injected stage runner, classify goes through runs.signals.run_task."""
    ingest, kdb, _ = classify_env
    _seed_item(kdb, "Validators offline across the network", "c1")
    ingest.stage_runner = None
    from runs.llm import base as llm_base
    from runs.llm.stub import StubProvider, scripted

    llm_base.registry.clear()
    provider = StubProvider(key="claude:subscription", responses=[scripted(
        text=json.dumps({"labels": [{"url_hash": "c1", "event_class": "outage",
                                     "assets": ["ETH"]}]}))])
    llm_base.registry.register(provider, replace=True)
    try:
        ingest.classify_news()
    finally:
        llm_base.registry.clear()
    row = kdb.execute("SELECT * FROM news_items WHERE url_hash='c1'").fetchone()
    assert row["event_class"] == "outage" and row["classified_by"] == "model"


def test_ingest_runs_the_scanner_in_pipeline_mode(ing, monkeypatch):
    ingest, _, kdb, root = ing
    ingest.cfg.signals.integration = "pipeline"
    ingest.cfg.signals.scanner.run_after_ingest = True
    seen = {}

    from runs.signals import pipeline as pipelinelib

    class _Report:
        candidates = 3
        new = ["a"]
        screened = []
        planned = []

    def fake_on_ingest(cfg, jdb, kdb_, **kwargs):
        seen["called"] = True
        return _Report()

    monkeypatch.setattr(pipelinelib, "on_ingest", fake_on_ingest)
    ingest._maybe_trigger()
    assert seen.get("called") is True
    row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='scanner'").fetchone()
    assert row is not None and row["status"] == "ok"


def test_legacy_integration_still_uses_the_trigger_engine(ing, monkeypatch):
    ingest, _, kdb, _ = ing
    ingest.cfg.signals.integration = "legacy"
    from runs import triggers as triggerslib

    monkeypatch.setattr(triggerslib, "evaluate_and_fire",
                        lambda *a, **k: {"fired": True, "reasons": ["news:hack"]})
    ingest._maybe_trigger()
    row = kdb.execute("SELECT * FROM ingest_runs WHERE phase='trigger'").fetchone()
    assert row is not None and row["status"] == "fired"


def test_a_scanner_failure_never_fails_ingest(ing, monkeypatch):
    ingest, _, _, _ = ing
    ingest.cfg.signals.integration = "pipeline"
    from runs.signals import pipeline as pipelinelib

    def boom(*a, **k):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(pipelinelib, "on_ingest", boom)
    ingest._maybe_trigger()   # must not raise


def test_classify_attempts_reach_the_journal(classify_env, dbs):
    """No ``jdb`` meant no ``llm_calls`` row — and therefore no breaker and no budget.

    The classifier routes through ``runs.llm.chain`` like every other task, but it reached
    the router with ``jdb=None``: nothing was journalled, ``tasks.classify.monthly_budget_usd``
    counted $0.00 for ever, no circuit breaker could ever open, and the console's AI &
    Models page showed a classifier that never ran.
    """
    ingest, kdb, _ = classify_env
    _, jdb, _ = dbs
    _seed_item(kdb, "Validators offline across the network", "j1")
    ingest.stage_runner = None
    from runs.llm import base as llm_base
    from runs.llm.stub import StubProvider, scripted

    llm_base.registry.clear()
    llm_base.registry.register(StubProvider(key="claude:subscription", responses=[
        scripted(text=json.dumps({"labels": [{"url_hash": "j1",
                                              "event_class": "outage",
                                              "assets": ["ETH"]}]}),
                 cost_usd=0.02, input_tokens=900, output_tokens=40)]), replace=True)
    try:
        ingest.classify_news()
    finally:
        llm_base.registry.clear()

    rows = [dict(r) for r in jdb.execute(
        "SELECT * FROM llm_calls WHERE task='classify' ORDER BY id")]
    assert rows, "a classify attempt wrote no llm_calls row"
    assert rows[0]["status"] == "ok"
    assert rows[0]["provider"] == "claude:subscription"
    assert rows[0]["cost_usd"] == pytest.approx(0.02)
    from runs.llm import chain as chain_mod

    month = NOW.strftime("%Y-%m")
    assert chain_mod.month_spend(jdb, month=month, task="classify") == pytest.approx(0.02)


def test_a_dead_credential_opens_the_classifier_circuit(classify_env, dbs):
    """Three auth failures in the window must take the credential out of the chain.

    Ingest runs every 15 minutes; without the journal the breaker could never open, so a
    revoked token was re-attempted for ever instead of being skipped.
    """
    ingest, kdb, _ = classify_env
    _, jdb, _ = dbs
    ingest.stage_runner = None
    from runs.llm import base as llm_base
    from runs.llm import health as health_mod
    from runs.llm.stub import StubProvider, scripted

    llm_base.registry.clear()
    llm_base.registry.register(
        StubProvider(key="claude:subscription",
                     default=scripted(failure="auth_error", error="401 invalid")),
        replace=True)
    try:
        for i in range(3):
            _seed_item(kdb, f"Validators offline, take {i}", f"d{i}")
            assert ingest._phase("classify", ingest.classify_news) is False
    finally:
        llm_base.registry.clear()

    assert health_mod.is_open(jdb, "claude:subscription", now=NOW), \
        "three auth failures left the credential's breaker closed"
