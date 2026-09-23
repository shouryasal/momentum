"""Deterministic feature builder — step 1 of the scanner (spec §2.1).

Everything a detector or a model is allowed to reason about is computed HERE, in plain
Python, from `knowledge.db`. The model never sees a raw table and never produces a number:
it receives this dict, and every figure it cites is checked back against
:meth:`Features.keys` before the answer is stored (``runs.signals.screener.verify``).

Keys are flat and stable: ``"BTC/USDT.rsi_4h"``, ``"global.regime_flip"``. A missing input
is ``None``, never a guess — a detector that needs a value it does not have simply does not
fire, which is the fail-closed behaviour the whole pipeline depends on.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ops.config import EarnConfig

__all__ = ["Features", "NewsRef", "build", "rsi", "sma", "true_range_pct"]

TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
GLOBAL = "global"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # -- access ---------------------------------------------------------------

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


# --------------------------------------------------------------------------- build


def _candles(kdb: sqlite3.Connection, pair: str, tf: str, limit: int) -> list[sqlite3.Row]:
    rows = kdb.execute(
        "SELECT open_time, open, high, low, close, volume FROM candles"
        " WHERE pair=? AND tf=? AND is_closed=1 ORDER BY open_time DESC LIMIT ?",
        (pair, tf, limit)).fetchall()
    return list(reversed(rows))


def _pair_features(kdb: sqlite3.Connection, cfg: EarnConfig, pair: str,
                   now: datetime) -> dict[str, float | None]:
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
    since = _iso(now - timedelta(hours=24))
    counts = kdb.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(corroborated),0) AS c FROM news_items"
        " WHERE COALESCE(published_at, fetched_at) >= ? AND assets LIKE ?",
        (since, f'%"{asset}"%')).fetchone()
    out["news_count_24h"] = float(counts["n"]) if counts else 0.0
    out["news_corroborated_24h"] = float(counts["c"]) if counts else 0.0
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
          jdb: sqlite3.Connection | None = None, root=None) -> Features:
    """Compute every feature for this cycle. Pure read; never writes, never raises on
    missing data (a missing input is ``None``)."""
    now = now or datetime.now(UTC)
    pairs = {p: _pair_features(kdb, cfg, p, now) for p in cfg.universe.pairs}
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
    return Features(ts_utc=_iso(now), pairs=pairs, globals=globals_,
                    news=_news_refs(kdb, cfg, now))
