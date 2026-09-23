"""15-minute ingest: incremental candles (incl. BNB/USDT data-only), order-book
snapshots, funding + open interest, whitelisted news RSS with the two-source
corroboration rule, the cheap classifier for items the keyword rules can't label
(config- and credential-gated; a classifier failure means the rule labels stand —
it never escalates), and the deterministic macro-blackout flag.

Phases run independently — one failing never skips the rest; each writes an
ingest_runs row and refreshes ``knowledge/state/freshness.json`` (the stdlib-readable
stamp the risk gate's staleness check reads — the knowledge DB is mounted read-only into
the containers while it is in WAL, so a SQLite read from there can block every entry).
Everything is idempotent (INSERT OR REPLACE / OR IGNORE keyed on natural keys;
re-fetching from the last stored candle corrects the previously in-progress one). HTTP is
injectable (httpx client) so tests use MockTransport.

Config, not code (spec §3): the ingested timeframes, the cold-start window and both news
keyword tables come from ``earn.yaml``; the module constants below are only the fallback
for a file that predates them. The classifier prompt is the tier-1 file named by
``research.stage_prompts.classify`` and routes through the chain router.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
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


def classify_schema(events: list[str], assets: list[str]) -> dict:
    """The classifier's output contract, built from the configured vocabularies."""
    return {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "url_hash": {"type": "string"},
                "event_class": {"type": ["string", "null"], "enum": [*events, None]},
                "assets": {"type": "array", "items": {"enum": list(assets)}},
            },
            "required": ["url_hash", "event_class", "assets"],
            "additionalProperties": False,
        }}},
        "required": ["labels"],
        "additionalProperties": False,
    }


#: Kept for callers that still import it; the live schema comes from ``classify_schema``.
CLASSIFY_SCHEMA = classify_schema(list(EVENT_KEYWORDS), list(ASSET_KEYWORDS))


def sym(pair: str) -> str:
    return pair.replace("/", "")


def now_iso(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ingest:
    def __init__(self, cfg: EarnConfig, kdb: sqlite3.Connection,
                 http: httpx.Client | None = None, now: datetime | None = None,
                 root: Path | None = None, stage_runner=None,
                 jdb: sqlite3.Connection | None = None):
        self.cfg = cfg
        self.kdb = kdb
        self.http = http or httpx.Client(timeout=10)
        self.now = now or datetime.now(UTC)
        self.root = root or REPO_ROOT
        self.stage_runner = stage_runner  # set -> legacy single-model call (tests, P3 shim)
        #: The journal the classifier's router attempts are written to. Optional only
        #: because most phases never touch a model; :meth:`_journal` opens the live
        #: journal for the duration of the classify call when one was not handed in.
        self.jdb = jdb

    # ------------------------------------------------------------------ vocabularies

    @property
    def event_keywords(self) -> dict[str, list[str]]:
        return dict(self.cfg.news.event_keywords or EVENT_KEYWORDS)

    @property
    def asset_keywords(self) -> dict[str, list[str]]:
        return dict(self.cfg.news.asset_keywords or ASSET_KEYWORDS)

    @property
    def timeframes(self) -> tuple[str, ...]:
        return tuple(self.cfg.ingest.timeframes or TFS)

    @property
    def cold_start_days(self) -> int:
        return int(self.cfg.ingest.cold_start_days or COLD_START_DAYS)

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
            ok = True
        except Exception as e:  # noqa: BLE001 — phases are isolated by design
            self._record(name, "error", str(e))
            print(f"ingest phase {name} failed: {e}", file=sys.stderr)
            ok = False
        self.write_freshness()
        return ok

    def freshness_sources(self) -> dict[str, datetime]:
        """The newest timestamp of every source the gate's staleness check watches."""
        out: dict[str, datetime] = {}
        book = self.kdb.execute(
            "SELECT MAX(captured_at) AS t FROM book_snapshots").fetchone()
        if book and book["t"]:
            try:
                out["book_snapshots"] = datetime.fromisoformat(
                    str(book["t"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        for tf in self.timeframes:
            row = self.kdb.execute(
                "SELECT MAX(open_time) AS t FROM candles WHERE tf=?", (tf,)).fetchone()
            if row and row["t"]:
                out[f"candles_{tf}"] = datetime.fromtimestamp(row["t"] / 1000, tz=UTC)
        news = self.kdb.execute("SELECT MAX(fetched_at) AS t FROM news_items").fetchone()
        if news and news["t"]:
            try:
                out["news"] = datetime.fromisoformat(str(news["t"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        return out

    def write_freshness(self) -> None:
        """Refresh ``knowledge/state/freshness.json`` after every phase.

        ``ops.lib.freshness`` owns the writer and the exact on-disk shape, and it is the
        ONLY writer: this used to carry a second implementation of the same file for the
        window before that module landed, which meant two places could disagree about the
        shape the gate's staleness check reads. Never raises — a freshness write failing
        must not fail an ingest phase.
        """
        try:
            sources = self.freshness_sources()
            if not sources:
                return
            from ops.lib import freshness as fresh

            fresh.record_many(sources, now=self.now, root=self.root)
        except Exception as e:  # noqa: BLE001 — telemetry only
            print(f"freshness write failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ candles

    def refresh_candles(self) -> None:
        pairs = [*self.cfg.universe.pairs, *self.cfg.universe.data_only_symbols]
        now_ms = int(self.now.timestamp() * 1000)
        for pair in pairs:
            for tf in self.timeframes:
                row = self.kdb.execute(
                    "SELECT MAX(open_time) FROM candles WHERE pair=? AND tf=?",
                    (pair, tf)).fetchone()
                start = row[0] if row[0] is not None else int(
                    (self.now - timedelta(days=self.cold_start_days)).timestamp() * 1000)
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
                    step = TF_MS.get(tf)
                    if step is None:
                        break  # a timeframe the stepper does not know: one page only
                    start = klines[-1][0] + step

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

    def classify_prompt(self, pending: list[dict]) -> str:
        """Render the tier-1 classify prompt (``research.stage_prompts.classify``)."""
        from runs.signals import stage_prompt_text

        template = stage_prompt_text(self.cfg, "classify", self.root)
        return (template
                .replace("{{EVENT_CLASSES}}", json.dumps(sorted(self.event_keywords)))
                .replace("{{ASSETS}}", json.dumps(sorted(self.asset_keywords)))
                .replace("{{ITEMS}}", json.dumps(pending, indent=2, sort_keys=True)))

    @contextmanager
    def _journal(self):
        """The journal connection the router needs, opened for this call if need be.

        ``run_task`` without a ``jdb`` writes no ``llm_calls`` row, gets no circuit
        breaker and no ``tasks.classify`` monthly budget: the classifier could then hammer
        a dead credential every 15 minutes for ever, spend outside every cap, and show the
        console an idle system. The journal is the ledger those three read, so this phase
        opens it exactly the way :meth:`_maybe_trigger` opens it for the scanner.

        A journal that cannot be opened yields ``None`` rather than failing the phase —
        the classifier never escalates, and losing the ledger must not lose the labels.
        """
        if self.jdb is not None:
            yield self.jdb
            return
        try:
            conn = db.connect(self.root / self.cfg.paths.journal_db)
        except Exception as e:  # noqa: BLE001 — see the docstring
            print(f"classify: journal unavailable ({e})", file=sys.stderr)
            yield None
            return
        try:
            yield conn
        finally:
            conn.close()

    def _classify_call(self, prompt: str, schema: dict) -> tuple[bool, str | None, str | None]:
        """One classify call. ``stage_runner`` set = the legacy single-model path;
        otherwise the chain router, so classify falls over to a local model like every
        other task."""
        if self.stage_runner is not None:
            from runs import router

            choice = router.resolve("classify",
                                    models_cfg=router.load_models_cfg(
                                        self.root / "config" / "models.yaml"))
            res = self.stage_runner(prompt, model=choice.model, max_turns=1,
                                    max_usd=choice.max_usd, effort=choice.effort,
                                    allowed_tools=[], output_schema=schema,
                                    deadline_s=120)
            return res.ok, res.text, res.meta.error
        from ops.models_config import load_models_cfg
        from runs.signals import run_task

        try:
            models_cfg = load_models_cfg(self.root / "config" / "models.yaml")
        except Exception:  # noqa: BLE001 — an unreadable models file is not fatal here
            models_cfg = None
        with self._journal() as jdb:
            outcome = run_task("classify", prompt, models_cfg=models_cfg,
                               output_schema=schema, tools_profile="none",
                               deadline_s=120, jdb=jdb, kdb=self.kdb, cfg=self.cfg,
                               root=self.root, now=self.now)
        return outcome.ok, outcome.text, outcome.error or outcome.failure

    def classify_news(self) -> None:
        """A cheap model labels the items the keyword rules can't (spec: the classifier
        never escalates — any failure leaves the rule labels standing)."""
        if not self.cfg.news.classify.enabled:
            return
        from ops.lib import claude_auth

        if claude_auth.resolve_any().source == "none":
            return
        events, assets_kw = self.event_keywords, self.asset_keywords
        since = now_iso(self.now - timedelta(hours=24))
        rows = self.kdb.execute(
            "SELECT url_hash, title FROM news_items WHERE fetched_at >= ?"
            " AND classified_by='rule' AND event_class IS NULL", (since,)).fetchall()
        pending = []
        for r in rows:
            title_l = r["title"].lower()
            if any(k in title_l for kws in events.values() for k in kws):
                continue  # the keyword rule will label it in corroborate
            pending.append({"url_hash": r["url_hash"], "title": r["title"]})
        if not pending:
            return
        pending = pending[:25]
        ok, text, error = self._classify_call(
            self.classify_prompt(pending), classify_schema(sorted(events), sorted(assets_kw)))
        if not ok or not text:
            raise RuntimeError(f"classifier failed: {error}")
        parsed = json.loads(text)
        labels = parsed.get("labels", []) if isinstance(parsed, dict) else parsed
        valid_hashes = {p["url_hash"] for p in pending}
        for item in labels:
            h = item.get("url_hash")
            ev = item.get("event_class")
            if h not in valid_hashes or (ev is not None and ev not in events):
                continue
            assets = sorted(a for a in (item.get("assets") or []) if a in assets_kw)
            self.kdb.execute(
                "UPDATE news_items SET event_class=?, assets=?, classified_by='model'"
                " WHERE url_hash=?", (ev, json.dumps(assets), h))
        self.kdb.commit()

    def corroborate(self) -> None:
        event_kw, asset_kw = self.event_keywords, self.asset_keywords
        since = now_iso(self.now - timedelta(hours=24))
        rows = self.kdb.execute(
            "SELECT id, url_hash, source, source_class, title, published_at,"
            " fetched_at, event_class, classified_by, assets AS assets_json"
            " FROM news_items WHERE fetched_at >= ?", (since,)).fetchall()
        items = []
        for r in rows:
            title_l = r["title"].lower()
            if r["classified_by"] == "model" and r["event_class"]:
                # the Haiku label stands — keywords could not classify this item
                assets = sorted(json.loads(r["assets_json"] or "[]"))
                event = r["event_class"]
            else:
                assets = sorted(a for a, kws in asset_kw.items()
                                if any(k in title_l for k in kws))
                event = next((ev for ev, kws in event_kw.items()
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

    # ------------------------------------------------------------------ claim check

    CLAIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")

    def claimcheck(self) -> None:
        """Deterministic credibility: a headline claiming an N% move is checked
        against realized candle moves (7d window before the item, 1d candles:
        max |single-day| and |cumulative|). Verified when the claim is within
        1.5x of what prices actually did; a claim prices never came close to is
        falsified. No claim, no single asset, or no data -> NULL (unjudged)."""
        since = now_iso(self.now - timedelta(hours=24))
        rows = self.kdb.execute(
            "SELECT id, title, assets, COALESCE(published_at, fetched_at) AS ts"
            " FROM news_items WHERE fetched_at >= ? AND claim_verified IS NULL"
            " AND assets IS NOT NULL", (since,)).fetchall()
        for r in rows:
            claims = [float(m) for m in self.CLAIM_RE.findall(r["title"])
                      if 2.0 <= float(m) <= 95.0]
            assets = json.loads(r["assets"] or "[]")
            if not claims or len(assets) != 1:
                continue
            pair = f"{assets[0]}/{self.cfg.universe.quote}"
            try:
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            end_ms = int(ts.timestamp() * 1000)
            start_ms = end_ms - 7 * 86_400_000
            candles = self.kdb.execute(
                "SELECT open, close FROM candles WHERE pair=? AND tf='1d'"
                " AND is_closed=1 AND open_time BETWEEN ? AND ?"
                " ORDER BY open_time", (pair, start_ms, end_ms)).fetchall()
            if len(candles) < 2:
                continue
            daily_max = max(abs(c["close"] / c["open"] - 1) * 100
                            for c in candles if c["open"])
            cumulative = abs(candles[-1]["close"] / candles[0]["open"] - 1) * 100 \
                if candles[0]["open"] else 0.0
            realized = max(daily_max, cumulative)
            verified = int(min(claims) <= realized * 1.5)
            self.kdb.execute("UPDATE news_items SET claim_verified=? WHERE id=?",
                             (verified, r["id"]))
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
                         ("classify", self.classify_news),
                         ("corroborate", self.corroborate),
                         ("claimcheck", self.claimcheck),
                         ("macro", self.update_macro_blackout)):
            ok = self._phase(name, fn) and ok
        self._maybe_trigger()
        return 0 if ok else 1

    def _maybe_trigger(self) -> None:
        """Post-ingest signal evaluation — fully isolated: a failure here never fails
        ingest (and never blocks the next cycle).

        ``signals.integration: pipeline`` with ``scanner.run_after_ingest`` runs one
        scanner cycle right after the data lands, so a */5 cron miss still gets a scan
        every 15 minutes. ``legacy`` keeps the original ``TriggerEngine.evaluate()``.
        """
        try:
            from runs import triggers as triggerslib

            if (self.cfg.signals.enabled
                    and self.cfg.signals.integration == "pipeline"
                    and self.cfg.signals.scanner.run_after_ingest):
                from runs.signals import pipeline as pipelinelib

                with db.connect(self.root / self.cfg.paths.journal_db) as jdb:
                    report = pipelinelib.on_ingest(self.cfg, jdb, self.kdb,
                                                   root=self.root, now=self.now)
                if report is not None:
                    self._record("scanner", "ok",
                                 f"candidates={report.candidates} new={len(report.new)}"
                                 f" screened={len(report.screened)}"
                                 f" planned={len(report.planned)}")
                return

            result = triggerslib.evaluate_and_fire(self.cfg, self.kdb,
                                                   root=self.root, now=self.now)
            if result and result["fired"]:
                self._record("trigger", "fired", ",".join(result["reasons"]))
        except Exception as e:  # noqa: BLE001
            print(f"signal evaluation failed: {e}", file=sys.stderr)


def main() -> int:
    cfg = load_config()
    with locks.acquire("ingest"):
        with db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return Ingest(cfg, kdb).run()


if __name__ == "__main__":
    sys.exit(main())
