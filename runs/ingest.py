"""15-minute ingest: incremental candles (incl. BNB/USDT data-only), order-book
snapshots, funding + open interest, whitelisted news RSS with the two-source
corroboration rule, and the deterministic macro-blackout flag.

Phases run independently — one failing never skips the rest; each writes an
ingest_runs row. Everything is idempotent (INSERT OR REPLACE / OR IGNORE keyed on
natural keys; re-fetching from the last stored candle corrects the previously
in-progress one). HTTP is injectable (httpx client) so tests use MockTransport.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser
import httpx
import yaml

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import locks

SPOT = "https://api.binance.com"
FUT = "https://fapi.binance.com"
TFS = ("1h", "4h", "1d")
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLD_START_DAYS = 7  # bulk history comes from ops/bootstrap_data.sh, not the API loop

EVENT_KEYWORDS = {
    "etf": ["etf"], "hack": ["hack", "exploit", "stolen", "breach"],
    "delist": ["delist"], "lawsuit": ["sec", "lawsuit", "charges", "settle"],
    "upgrade": ["upgrade", "fork", "hard fork"], "outage": ["outage", "halt", "paused"],
    "depeg": ["depeg", "de-peg"], "macro": ["fomc", "cpi", "rate cut", "rate hike", "fed"],
    "listing": ["listing", "lists "], "liquidation": ["liquidation", "liquidated"],
}
ASSET_KEYWORDS = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth ", "ether "]}


def sym(pair: str) -> str:
    return pair.replace("/", "")


def now_iso(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ingest:
    def __init__(self, cfg: EarnConfig, kdb: sqlite3.Connection,
                 http: httpx.Client | None = None, now: datetime | None = None,
                 root: Path | None = None):
        self.cfg = cfg
        self.kdb = kdb
        self.http = http or httpx.Client(timeout=10)
        self.now = now or datetime.now(UTC)
        self.root = root or REPO_ROOT

    # ------------------------------------------------------------------ helpers

    def _get(self, url: str, params: dict) -> httpx.Response:
        r = self.http.get(url, params=params)
        r.raise_for_status()
        used = r.headers.get("x-mbx-used-weight-1m")
        if used and int(used) > 3000:  # ~50% of the 6000/min IP budget
            raise RuntimeError(f"rate budget guard: used-weight-1m={used}")
        return r

    def _record(self, phase: str, status: str, detail: str = "") -> None:
        self.kdb.execute(
            "INSERT OR REPLACE INTO ingest_runs(job, phase, window_start, started_at,"
            " finished_at, status, detail) VALUES ('ingest',?,?,?,?,?,?)",
            (phase, self.now.strftime("%Y-%m-%dT%H:%M"), now_iso(self.now),
             now_iso(datetime.now(UTC)), status, detail[:500]),
        )
        self.kdb.commit()

    def _phase(self, name: str, fn) -> bool:
        try:
            fn()
            self._record(name, "ok")
            return True
        except Exception as e:  # noqa: BLE001 — phases are isolated by design
            self._record(name, "error", str(e))
            print(f"ingest phase {name} failed: {e}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------ candles

    def refresh_candles(self) -> None:
        pairs = [*self.cfg.universe.pairs, *self.cfg.universe.data_only_symbols]
        now_ms = int(self.now.timestamp() * 1000)
        for pair in pairs:
            for tf in TFS:
                row = self.kdb.execute(
                    "SELECT MAX(open_time) FROM candles WHERE pair=? AND tf=?",
                    (pair, tf)).fetchone()
                start = row[0] if row[0] is not None else int(
                    (self.now - timedelta(days=COLD_START_DAYS)).timestamp() * 1000)
                while True:
                    r = self._get(f"{SPOT}/api/v3/klines",
                                  {"symbol": sym(pair), "interval": tf,
                                   "startTime": start, "limit": 1000})
                    klines = r.json()
                    if not klines:
                        break
                    self.kdb.executemany(
                        "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high,"
                        " low, close, volume, quote_volume, close_time, is_closed)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [(pair, tf, k[0], float(k[1]), float(k[2]), float(k[3]),
                          float(k[4]), float(k[5]), float(k[7]), k[6],
                          int(k[6] < now_ms)) for k in klines],
                    )
                    self.kdb.commit()
                    if len(klines) < 1000:
                        break
                    start = klines[-1][0] + TF_MS[tf]

    # ------------------------------------------------------------------ books

    def snapshot_books(self) -> None:
        for pair in self.cfg.universe.pairs:
            r = self._get(f"{SPOT}/api/v3/depth", {"symbol": sym(pair), "limit": 100})
            book = r.json()
            bids = [(float(p), float(q)) for p, q in book["bids"]]
            asks = [(float(p), float(q)) for p, q in book["asks"]]
            if not bids or not asks:
                continue
            bb, ba = bids[0][0], asks[0][0]
            mid = (bb + ba) / 2
            self.kdb.execute(
                "INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
                " spread_bps, bid_depth_05pct, ask_depth_05pct, levels_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (pair, now_iso(self.now), bb, ba, mid, (ba - bb) / mid * 1e4,
                 sum(q for p, q in bids if p >= mid * 0.995),
                 sum(q for p, q in asks if p <= mid * 1.005),
                 json.dumps({"bids": bids[:20], "asks": asks[:20]})),
            )
        self.kdb.commit()

    # ------------------------------------------------------------------ funding + OI

    def refresh_funding(self) -> None:
        for pair in self.cfg.universe.pairs:
            s = sym(pair)
            prem = self._get(f"{FUT}/fapi/v1/premiumIndex", {"symbol": s}).json()
            self.kdb.execute(
                "INSERT OR REPLACE INTO funding_current(symbol, last_rate,"
                " next_funding_time, mark_price, updated_at) VALUES (?,?,?,?,?)",
                (s, float(prem.get("lastFundingRate", 0)),
                 prem.get("nextFundingTime"), float(prem.get("markPrice", 0)),
                 now_iso(self.now)),
            )
            hist = self._get(f"{FUT}/fapi/v1/fundingRate", {"symbol": s, "limit": 16}).json()
            self.kdb.executemany(
                "INSERT OR REPLACE INTO funding(symbol, funding_time, rate, mark_price)"
                " VALUES (?,?,?,?)",
                [(s, h["fundingTime"], float(h["fundingRate"]),
                  float(h.get("markPrice") or 0)) for h in hist],
            )
            oi = self._get(f"{FUT}/fapi/v1/openInterest", {"symbol": s}).json()
            self.kdb.execute(
                "INSERT OR REPLACE INTO open_interest(symbol, ts_utc, oi, oi_value_usdt)"
                " VALUES (?,?,?,?)",
                (s, now_iso(self.now), float(oi.get("openInterest", 0)), None),
            )
        self.kdb.commit()

    # ------------------------------------------------------------------ news

    def pull_news(self) -> None:
        for feed in self.cfg.news.whitelist:
            state = self.kdb.execute(
                "SELECT etag, modified, fail_count FROM feed_state WHERE feed_url=?",
                (feed.url,)).fetchone()
            headers = {}
            if state and state["etag"]:
                headers["If-None-Match"] = state["etag"]
            if state and state["modified"]:
                headers["If-Modified-Since"] = state["modified"]
            try:
                r = self.http.get(feed.url, headers=headers, follow_redirects=True)
                if r.status_code == 304:
                    continue
                r.raise_for_status()
            except Exception:
                self.kdb.execute(
                    "INSERT INTO feed_state(feed_url, fail_count) VALUES (?,1)"
                    " ON CONFLICT(feed_url) DO UPDATE SET fail_count=fail_count+1",
                    (feed.url,))
                self.kdb.commit()
                continue
            parsed = feedparser.parse(r.content)
            for e in parsed.entries[:50]:
                link = getattr(e, "link", None)
                title = getattr(e, "title", None)
                if not link or not title:
                    continue
                h = hashlib.sha256(link.encode()).hexdigest()
                published = None
                if getattr(e, "published_parsed", None):
                    published = datetime(*e.published_parsed[:6], tzinfo=UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                self.kdb.execute(
                    "INSERT OR IGNORE INTO news_items(url_hash, source, source_class,"
                    " title, url, published_at, fetched_at, classified_by)"
                    " VALUES (?,?,?,?,?,?,?, 'rule')",
                    (h, feed.name, feed.class_, title, link, published, now_iso(self.now)),
                )
            self.kdb.execute(
                "INSERT INTO feed_state(feed_url, etag, modified, last_ok, fail_count)"
                " VALUES (?,?,?,?,0)"
                " ON CONFLICT(feed_url) DO UPDATE SET etag=excluded.etag,"
                " modified=excluded.modified, last_ok=excluded.last_ok, fail_count=0",
                (feed.url, r.headers.get("etag"), r.headers.get("last-modified"),
                 now_iso(self.now)),
            )
        self.kdb.commit()

    def corroborate(self) -> None:
        since = now_iso(self.now - timedelta(hours=24))
        rows = self.kdb.execute(
            "SELECT id, url_hash, source, source_class, title, published_at, fetched_at"
            " FROM news_items WHERE fetched_at >= ?", (since,)).fetchall()
        items = []
        for r in rows:
            title_l = r["title"].lower()
            assets = sorted(a for a, kws in ASSET_KEYWORDS.items()
                            if any(k in title_l for k in kws))
            event = next((ev for ev, kws in EVENT_KEYWORDS.items()
                          if any(k in title_l for k in kws)), None)
            ts = r["published_at"] or r["fetched_at"]
            bucket = ts[:11] + ("00" if ts[11:13] < "12" else "12")
            tokens = frozenset(re.findall(r"[a-z]{4,}", title_l))
            items.append({**dict(r), "assets": assets, "event": event,
                          "bucket": bucket, "tokens": tokens})
        clusters: dict[str, list[dict]] = {}
        for it in items:
            if it["event"]:
                key = f"{','.join(it['assets'])}|{it['event']}|{it['bucket']}"
            else:
                key = None
                for cid, members in clusters.items():
                    if cid.startswith("jac|") and members:
                        m = members[0]
                        inter = len(it["tokens"] & m["tokens"])
                        union = len(it["tokens"] | m["tokens"]) or 1
                        if inter / union >= 0.5:
                            key = cid
                            break
                if key is None:
                    key = f"jac|{it['url_hash'][:12]}"
            clusters.setdefault(key, []).append(it)
        for cid, members in clusters.items():
            sources = {m["source"] for m in members if m["source_class"] == "secondary"}
            for m in members:
                corroborated = int(
                    m["source_class"] == "primary" or len(sources) >= self.cfg.news.min_sources
                )
                self.kdb.execute(
                    "UPDATE news_items SET assets=?, event_class=?, cluster_id=?,"
                    " corroborated=?, corroborating_sources=? WHERE id=?",
                    (json.dumps(m["assets"]), m["event"], cid, corroborated,
                     max(len(sources), 1), m["id"]),
                )
        self.kdb.commit()

    # ------------------------------------------------------------------ macro blackout

    def update_macro_blackout(self) -> None:
        cal_path = self.root / "config" / "macro_calendar.yaml"
        flags_path = self.root / self.cfg.paths.flags_file
        window = timedelta(minutes=self.cfg.risk.blackout.window_minutes)
        events = []
        if cal_path.exists():
            events = (yaml.safe_load(cal_path.read_text()) or {}).get("events", [])
        active = None
        for ev in events:
            at = datetime.fromisoformat(str(ev["at"]).replace("Z", "+00:00"))
            if at - window <= self.now <= at + window:
                active = (ev["name"], at + window)
                break
        try:
            current = flagslib.active_flags(flags_path, self.now)
        except flagslib.FlagsError:
            current = {}
        if active:
            name, until = active
            flagslib.set_flag(flags_path, "macro_blackout", severity="block_entries",
                              reason=name, set_by="ingest",
                              expires_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              now=self.now, audit_conn=self.kdb)
        else:
            mb = current.get("macro_blackout")
            if mb and mb.get("set_by") == "ingest":
                flagslib.clear_flag(flags_path, "macro_blackout", by="ingest",
                                    now=self.now, audit_conn=self.kdb)
            else:
                # deterministic heartbeat so the gate's 24h flags-staleness check
                # never fires while ingest is healthy
                flagslib.touch(flags_path, now=self.now)

    # ------------------------------------------------------------------ main

    def run(self) -> int:
        ok = True
        for name, fn in (("candles", self.refresh_candles),
                         ("books", self.snapshot_books),
                         ("funding", self.refresh_funding),
                         ("news", self.pull_news),
                         ("corroborate", self.corroborate),
                         ("macro", self.update_macro_blackout)):
            ok = self._phase(name, fn) and ok
        return 0 if ok else 1


def main() -> int:
    cfg = load_config()
    with locks.acquire("ingest"):
        with db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return Ingest(cfg, kdb).run()


if __name__ == "__main__":
    sys.exit(main())
