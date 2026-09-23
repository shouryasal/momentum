"""Deterministic feature builder — step 1 of the scanner (spec §2.1).

Everything a detector or a model is allowed to reason about is computed HERE, in plain
Python, from `knowledge.db`. The model never sees a raw table and never produces a number:
it receives this dict, and every figure it cites is checked back against
:meth:`Features.keys` before the answer is stored (``runs.signals.screener.verify``).

Keys are flat and stable: ``"BTC/USDT.rsi_4h"``, ``"global.regime_flip"``. A missing input
is ``None``, never a guess — a detector that needs a value it does not have simply does not
fire, which is the fail-closed behaviour the whole pipeline depends on.

**Two tiers** (docs/design/wide-universe.md §5.2). Under a wide universe the binding
constraint is tokens, not storage or I/O: today's 24 keys per pair rendered over a
106-pair watchlist is ~22,500 tokens, which exceeds ``budgets.context_tokens.research``
(20,000) with the features block *alone*. So:

* the **cheap tier** (:data:`CHEAP_KEYS`, 6 keys off the daily candles plus one batched
  news query) is computed for the WHOLE watchlist — ~2,800 tokens at 106 pairs;
* the **rich tier** (every key below) is computed only for the core assets, whatever the
  sleeves currently hold, and the handful of watchlist names the cheap tier says are
  actually moving — :func:`rank_watchlist` picks those, deterministically.

A cheap-tier pair therefore carries a strict SUBSET of the rich keys, never a different
value for the same key, so a detector reading a key a cheap pair does not have sees
``None`` and does not fire — the existing fail-closed path, not a new one.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ops.config import EarnConfig

__all__ = [
    "CHEAP_KEYS",
    "DEFAULT_RICH_PAIRS",
    "DEFAULT_WATCHLIST_MAX",
    "Features",
    "NewsRef",
    "build",
    "core_pairs",
    "held_pairs",
    "rank_watchlist",
    "rsi",
    "sma",
    "true_range_pct",
    "watchlist_pairs",
]

TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
GLOBAL = "global"

#: The cheap tier: what the scanner carries for every name on the watchlist. Five of the
#: six are the keys the wide-universe study costed (§5.2); ``close`` is added because the
#: other five are unreadable without a price to hang them on.
CHEAP_KEYS: tuple[str, ...] = (
    "close", "ret_24h", "vol_ann_20d", "ma200_dist_pct", "dip_from_high_pct",
    "news_count_24h",
)

#: How many pairs get the full 24-key treatment. Core + held always do; the rest of the
#: budget goes to the highest-ranked watchlist names. 20 pairs ≈ 3,400 tokens.
DEFAULT_RICH_PAIRS = 20

#: A hard stop on the watchlist the scanner will render, whatever the resolver says. It is
#: not a policy knob — it is the thing that keeps a resolver bug out of the token budget.
DEFAULT_WATCHLIST_MAX = 150


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(value: Any) -> Any:
    """Six significant figures for a float; everything else untouched."""
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


@dataclass(frozen=True)
class NewsRef:
    """One corroborated news item the screener may cite, by hash."""

    url_hash: str
    title: str
    source: str
    event_class: str | None
    assets: tuple[str, ...]
    corroborated: bool
    published_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "news_hash": self.url_hash, "title": self.title, "source": self.source,
            "event_class": self.event_class, "assets": list(self.assets),
            "corroborated": self.corroborated, "published_at": self.published_at,
        }


@dataclass(frozen=True)
class Features:
    """The whole computed picture for one scan cycle."""

    ts_utc: str
    pairs: dict[str, dict[str, float | None]] = field(default_factory=dict)
    globals: dict[str, float | str | None] = field(default_factory=dict)
    news: tuple[NewsRef, ...] = ()
    #: The pairs that got the rich tier. The rest of ``pairs`` carry :data:`CHEAP_KEYS`.
    rich: tuple[str, ...] = ()

    # -- access ---------------------------------------------------------------

    def tier(self, pair: str) -> str:
        return "rich" if pair in self.rich else "cheap"

    def render(self) -> str:
        """The ``{{FEATURES}}`` block: compact JSON, six significant figures.

        Two measured savings, neither of which costs the model anything it can use.
        Pretty-printing the same dict costs 24% more tokens (wide-universe §5.2). And a
        raw float renders as ``1.4285714285714286`` — eighteen characters of which six are
        information; at 106 pairs that alone is about 4,000 tokens of trailing digits.

        The HOST keeps full precision: this is the presentation, not the record. Keys stay
        flat and sorted, so a cited key is still a literal substring of what was shown.
        """
        return json.dumps({k: _round(v) for k, v in self.flat().items()},
                          sort_keys=True, separators=(",", ":"), default=str)

    def flat(self) -> dict[str, Any]:
        out: dict[str, Any] = {f"{GLOBAL}.{k}": v for k, v in self.globals.items()}
        for pair, values in self.pairs.items():
            for k, v in values.items():
                out[f"{pair}.{k}"] = v
        return out

    def keys(self) -> set[str]:
        """Every citable feature key. The host's hallucination check uses exactly this."""
        return set(self.flat())

    def news_hashes(self) -> set[str]:
        return {n.url_hash for n in self.news}

    def get(self, pair: str | None, name: str) -> float | None:
        bucket = self.globals if pair is None else self.pairs.get(pair, {})
        value = bucket.get(name)
        return value if isinstance(value, (int, float)) else None

    def key(self, pair: str | None, name: str) -> str:
        return f"{pair or GLOBAL}.{name}"

    def as_dict(self) -> dict[str, Any]:
        return {"ts_utc": self.ts_utc, "globals": dict(self.globals),
                "pairs": {p: dict(v) for p, v in self.pairs.items()},
                "rich": list(self.rich),
                "news": [n.as_dict() for n in self.news]}


# --------------------------------------------------------------------------- math


def sma(values: list[float], length: int) -> float | None:
    if len(values) < length or length <= 0:
        return None
    return sum(values[-length:]) / length


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. ``None`` until there are ``period + 1`` closes."""
    if len(closes) < period + 1 or period < 2:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    seed = deltas[:period]
    gains = sum(d for d in seed if d > 0) / period
    losses = sum(-d for d in seed if d < 0) / period
    for d in deltas[period:]:
        gains = (gains * (period - 1) + max(d, 0.0)) / period
        losses = (losses * (period - 1) + max(-d, 0.0)) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def true_range_pct(rows: list[sqlite3.Row], period: int = 14) -> float | None:
    """ATR over ``period`` candles, expressed as a percentage of the last close."""
    if len(rows) < period + 1:
        return None
    trs = []
    for prev, cur in zip(rows[-period - 1:-1], rows[-period:], strict=True):
        hi, lo, pc = cur["high"], cur["low"], prev["close"]
        if hi is None or lo is None or pc is None:
            return None
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    last = rows[-1]["close"]
    if not last:
        return None
    return sum(trs) / len(trs) / last * 100.0


def _zscore(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    sample, last = values[:-1], values[-1]
    mean = sum(sample) / len(sample)
    var = sum((v - mean) ** 2 for v in sample) / len(sample)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (last - mean) / sd


def _pct(open_: float | None, close: float | None) -> float | None:
    if not open_ or close is None:
        return None
    return (close / open_ - 1.0) * 100.0


# --------------------------------------------------------------------------- universe


def _pairs_of(cfg: EarnConfig, name: str) -> tuple[str, ...]:
    """A universe list by attribute name, tolerating a config that has not grown it yet.

    U1 turns ``universe.assets``/``pairs`` into computed properties over the point-in-time
    snapshot and adds ``watchlist_pairs``/``tradeable_pairs``/``core``. This module reads
    whichever of those exist, so it is correct both before and after that lands and never
    needs to know which package shipped first.
    """
    value = getattr(cfg.universe, name, None)
    if not value:
        return ()
    quote = cfg.universe.quote
    return tuple(p if "/" in str(p) else f"{p}/{quote}" for p in value)


def _budget(cfg: EarnConfig, name: str, default: int) -> int:
    value = getattr(getattr(cfg.signals, "scanner", None), name, None)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def watchlist_pairs(cfg: EarnConfig) -> tuple[str, ...]:
    """Every pair the scanner may look at, capped at :data:`DEFAULT_WATCHLIST_MAX`."""
    pairs = _pairs_of(cfg, "watchlist_pairs") or _pairs_of(cfg, "pairs")
    return pairs[:_budget(cfg, "watchlist_max", DEFAULT_WATCHLIST_MAX)]


def core_pairs(cfg: EarnConfig) -> tuple[str, ...]:
    """BTC/ETH under the wide universe; every configured pair before U1 lands."""
    return _pairs_of(cfg, "core") or _pairs_of(cfg, "pairs")


def held_pairs(jdb: sqlite3.Connection | None, cfg: EarnConfig) -> tuple[str, ...]:
    """Pairs the sleeves currently hold, best effort. A read failure means "none".

    Held names always get the rich tier: the one thing worse than not noticing a move on a
    coin we do not own is not noticing one on a coin we do.
    """
    if jdb is None:
        return ()
    quote = cfg.universe.quote
    out: list[str] = []
    for sql in ("SELECT positions_json FROM nav_points WHERE ts_utc ="
                " (SELECT MAX(ts_utc) FROM nav_points)",
                "SELECT positions_json FROM nav_daily WHERE date_utc ="
                " (SELECT MAX(date_utc) FROM nav_daily)"):
        try:
            rows = jdb.execute(sql).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            try:
                positions = json.loads(row["positions_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(positions, dict):
                continue
            for key, amount in positions.items():
                if not isinstance(amount, (int, float)) or amount <= 0:
                    continue
                base = str(key).split("/")[0].upper()
                if base == quote.upper():
                    continue          # cash is a balance, not a position to watch
                pair = key if "/" in str(key) else f"{base}/{quote}"
                if pair not in out:
                    out.append(pair)
        if out:
            break
    return tuple(out)


def rank_watchlist(cheap: dict[str, dict[str, float | None]], *,
                   exclude: tuple[str, ...] = ()) -> list[str]:
    """Order the watchlist by how much it is actually doing, from the cheap tier alone.

    This is the "prioritise, do not scan everything" rule made deterministic. It is an
    ATTENTION score, not a forecast, and deliberately so: the wide-universe study measured
    90-day cross-sectional momentum at a rank IC of −0.067 (t = −7.4), so anything that
    ranked coins by trailing return would be spending the token budget on a signal known to
    point the wrong way. Magnitude, not direction: a −20% day earns a look exactly as much
    as a +20% one. Ties break on the pair name so two runs of the same cycle agree.

    A pair with no price is **omitted**, not ranked last: there is nothing to rank, and a
    rich tier over it would be twenty-four nulls. That is exactly what a freshly resolved
    watchlist looks like before its candles have been downloaded, and without this rule
    the whole rich budget goes to coins the system has never seen a single bar of.
    """
    skip = set(exclude)

    def score(values: dict[str, float | None]) -> float:
        ret = abs(values.get("ret_24h") or 0.0)
        dip = max(values.get("dip_from_high_pct") or 0.0, 0.0)
        ma = abs(values.get("ma200_dist_pct") or 0.0)
        news = min(values.get("news_count_24h") or 0.0, 10.0)
        return ret * 2.0 + dip * 0.5 + ma * 0.1 + news * 1.5

    return sorted((p for p in cheap
                   if p not in skip and cheap[p].get("close") is not None),
                  key=lambda p: (-score(cheap[p]), p))


# --------------------------------------------------------------------------- build


def _candles(kdb: sqlite3.Connection, pair: str, tf: str, limit: int) -> list[sqlite3.Row]:
    rows = kdb.execute(
        "SELECT open_time, open, high, low, close, volume FROM candles"
        " WHERE pair=? AND tf=? AND is_closed=1 ORDER BY open_time DESC LIMIT ?",
        (pair, tf, limit)).fetchall()
    return list(reversed(rows))


def _vol_ann_20d(closes: list[float]) -> float | None:
    """Annualised stdev of the last 20 daily log returns, in percent. Crypto trades 365."""
    if len(closes) < 21:
        return None
    rets = []
    for prev, cur in zip(closes[-21:-1], closes[-20:], strict=True):
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.0) * 100.0


def _news_counts(kdb: sqlite3.Connection, now: datetime) -> dict[str, tuple[float, float]]:
    """``{asset: (items, corroborated)}`` over the 24h window, in ONE query.

    Per-pair ``assets LIKE '%"BTC"%'`` scans were 2 queries per pair per cycle; at 288
    cycles a day over a 106-pair watchlist that is a quarter of a million reads competing
    with the ingest job for the write lock (wide-universe §5.2).
    """
    since = _iso(now - timedelta(hours=24))
    counts: dict[str, list[float]] = {}
    try:
        rows = kdb.execute(
            "SELECT assets, corroborated FROM news_items"
            " WHERE COALESCE(published_at, fetched_at) >= ?", (since,)).fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        try:
            assets = json.loads(row["assets"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for asset in assets if isinstance(assets, list) else ():
            if not isinstance(asset, str):
                continue
            bucket = counts.setdefault(asset, [0.0, 0.0])
            bucket[0] += 1.0
            bucket[1] += 1.0 if row["corroborated"] else 0.0
    return {a: (n, c) for a, (n, c) in counts.items()}


def _cheap_pair_features(kdb: sqlite3.Connection, cfg: EarnConfig, pair: str,
                         news: dict[str, tuple[float, float]]) -> dict[str, float | None]:
    """:data:`CHEAP_KEYS` for one pair: daily candles only, no funding, no book, no news SQL."""
    det = cfg.signals.scanner.detectors
    d1 = _candles(kdb, pair, "1d", max(det.dip_from_high.lookback_days, 260))
    closes = [r["close"] for r in d1 if r["close"] is not None]
    last = closes[-1] if closes else None
    ma200 = sma(closes, 200)
    dip_window = d1[-det.dip_from_high.lookback_days:]
    high = max((r["high"] for r in dip_window if r["high"] is not None), default=None)
    n, _c = news.get(pair.split("/")[0], (0.0, 0.0))
    return {
        "close": last,
        "ret_24h": _pct(d1[-1]["open"], d1[-1]["close"]) if d1 else None,
        "vol_ann_20d": _vol_ann_20d(closes),
        "ma200_dist_pct": ((last / ma200 - 1) * 100.0) if (ma200 and last) else None,
        "dip_from_high_pct": ((1 - last / high) * 100.0) if (high and last) else None,
        "news_count_24h": n,
    }


def _pair_features(kdb: sqlite3.Connection, cfg: EarnConfig, pair: str,
                   now: datetime,
                   news: dict[str, tuple[float, float]] | None = None,
                   ) -> dict[str, float | None]:
    det = cfg.signals.scanner.detectors
    out: dict[str, float | None] = {}

    h1 = _candles(kdb, pair, "1h", max(det.volume_spike.lookback + 2, 200))
    h4 = _candles(kdb, pair, "4h", max(det.rsi_extreme.period + 60, 120))
    d1 = _candles(kdb, pair, "1d", max(det.ma_cross.slow + 5, det.dip_from_high.lookback_days,
                                       det.breakout.lookback, 260))

    out["close"] = h1[-1]["close"] if h1 else (d1[-1]["close"] if d1 else None)
    out["ret_1h"] = _pct(h1[-1]["open"], h1[-1]["close"]) if h1 else None
    out["ret_4h"] = _pct(h4[-1]["open"], h4[-1]["close"]) if h4 else None
    out["ret_24h"] = _pct(d1[-1]["open"], d1[-1]["close"]) if d1 else None

    closes4 = [r["close"] for r in h4 if r["close"] is not None]
    out["rsi_4h"] = rsi(closes4, det.rsi_extreme.period)
    out["atr_pct_4h"] = true_range_pct(h4, 14)

    vols = [r["volume"] for r in h1[-det.volume_spike.lookback - 1:] if r["volume"] is not None]
    out["vol_z_1h"] = _zscore(vols)
    mean_vol = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else None
    out["rvol_1h"] = (vols[-1] / mean_vol) if (mean_vol and vols) else None

    closes1d = [r["close"] for r in d1 if r["close"] is not None]
    out["vol_ann_20d"] = _vol_ann_20d(closes1d)
    ma200 = sma(closes1d, 200)
    out["ma200_1d"] = ma200
    out["ma200_dist_pct"] = ((closes1d[-1] / ma200 - 1) * 100.0) if (ma200 and closes1d) else None
    out["ma_fast_1d"] = sma(closes1d, det.ma_cross.fast)
    out["ma_slow_1d"] = sma(closes1d, det.ma_cross.slow)
    prev_closes = closes1d[:-1]
    out["ma_fast_1d_prev"] = sma(prev_closes, det.ma_cross.fast)
    out["ma_slow_1d_prev"] = sma(prev_closes, det.ma_cross.slow)

    look = det.breakout.lookback
    window = d1[-look - 1:-1] if len(d1) > look else []
    out["range_high_20d"] = max((r["high"] for r in window if r["high"] is not None),
                                default=None)
    out["range_low_20d"] = min((r["low"] for r in window if r["low"] is not None),
                               default=None)

    dip_window = d1[-det.dip_from_high.lookback_days:]
    high = max((r["high"] for r in dip_window if r["high"] is not None), default=None)
    out["high_30d"] = high
    last_close = closes1d[-1] if closes1d else None
    out["dip_from_high_pct"] = ((1 - last_close / high) * 100.0) if (high and last_close) else None
    out["drawdown_pct"] = out["dip_from_high_pct"]

    symbol = pair.replace("/", "")
    fund = kdb.execute("SELECT last_rate FROM funding_current WHERE symbol=?",
                       (symbol,)).fetchone()
    out["funding_8h"] = float(fund["last_rate"]) if fund and fund["last_rate"] is not None else None

    oi = kdb.execute("SELECT oi FROM open_interest WHERE symbol=? ORDER BY ts_utc DESC LIMIT 2",
                     (symbol,)).fetchall()
    if len(oi) == 2 and oi[1]["oi"]:
        out["oi_delta_pct"] = (oi[0]["oi"] / oi[1]["oi"] - 1) * 100.0
    else:
        out["oi_delta_pct"] = None

    book = kdb.execute(
        "SELECT spread_bps FROM book_snapshots WHERE pair=? ORDER BY captured_at DESC LIMIT 1",
        (pair,)).fetchone()
    out["spread_bps"] = float(book["spread_bps"]) if book else None

    asset = pair.split("/")[0]
    if news is None:
        news = _news_counts(kdb, now)
    n, c = news.get(asset, (0.0, 0.0))
    out["news_count_24h"] = n
    out["news_corroborated_24h"] = c
    return out


def _news_refs(kdb: sqlite3.Connection, cfg: EarnConfig, now: datetime) -> tuple[NewsRef, ...]:
    since = _iso(now - timedelta(hours=24))
    rows = kdb.execute(
        "SELECT url_hash, title, source, event_class, assets, corroborated, published_at"
        " FROM news_items WHERE COALESCE(published_at, fetched_at) >= ?"
        " ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 60", (since,)).fetchall()
    refs = []
    for r in rows:
        try:
            assets = tuple(json.loads(r["assets"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            assets = ()
        refs.append(NewsRef(url_hash=r["url_hash"], title=r["title"], source=r["source"],
                            event_class=r["event_class"], assets=assets,
                            corroborated=bool(r["corroborated"]),
                            published_at=r["published_at"]))
    return tuple(refs)


def build(kdb: sqlite3.Connection, cfg: EarnConfig, *, now: datetime | None = None,
          jdb: sqlite3.Connection | None = None, root=None,
          rich_pairs: int | None = None) -> Features:
    """Compute every feature for this cycle. Pure read; never writes, never raises on
    missing data (a missing input is ``None``).

    Two passes: the cheap tier over the whole watchlist, then the rich tier for core,
    held and the highest-ranked names the cheap tier surfaced. See the module docstring.
    """
    now = now or datetime.now(UTC)
    news = _news_counts(kdb, now)
    watchlist = watchlist_pairs(cfg)
    cheap = {p: _cheap_pair_features(kdb, cfg, p, news) for p in watchlist}

    budget = _budget(cfg, "rich_pairs", DEFAULT_RICH_PAIRS) if rich_pairs is None \
        else max(1, int(rich_pairs))
    # Core and held are rich whatever the watchlist says: a core asset that fell off the
    # resolver's list must not also fall out of the scanner's sight.
    rich: list[str] = []
    for pair in (*core_pairs(cfg), *held_pairs(jdb, cfg)):
        if pair not in rich:
            rich.append(pair)
    for pair in rank_watchlist(cheap, exclude=tuple(rich)):
        if len(rich) >= budget:
            break
        rich.append(pair)

    pairs = dict(cheap)
    for pair in rich:
        pairs[pair] = _pair_features(kdb, cfg, pair, now, news)
    globals_: dict[str, float | str | None] = {}

    cur = kdb.execute("SELECT regime, ts_utc FROM state_snapshots WHERE regime IS NOT NULL"
                      " ORDER BY ts_utc DESC LIMIT 1").fetchone()
    globals_["regime"] = cur["regime"] if cur else None
    globals_["regime_asof"] = cur["ts_utc"] if cur else None

    last_decide = None
    if jdb is not None:
        try:
            row = jdb.execute(
                "SELECT MAX(started_utc) AS t FROM runs WHERE stage='decide'").fetchone()
            last_decide = row["t"] if row else None
        except sqlite3.Error:
            last_decide = None
    globals_["last_decide_utc"] = last_decide
    prev = None
    if last_decide:
        row = kdb.execute(
            "SELECT regime FROM state_snapshots WHERE regime IS NOT NULL AND ts_utc <= ?"
            " ORDER BY ts_utc DESC LIMIT 1", (last_decide,)).fetchone()
        prev = row["regime"] if row else None
    globals_["regime_at_last_decide"] = prev
    globals_["regime_flip"] = float(bool(prev and cur and prev != cur["regime"]))
    globals_["watchlist_size"] = float(len(pairs))
    globals_["rich_pairs"] = float(len(rich))
    return Features(ts_utc=_iso(now), pairs=pairs, globals=globals_,
                    news=_news_refs(kdb, cfg, now), rich=tuple(rich))
