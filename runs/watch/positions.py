"""Every number the watcher reasons about, computed in Python. No model is involved.

This is the first half of the watcher and by far the more important one. For each open
position it computes price against entry, stop and take-profit, unrealised P&L, time held,
the position's weight against its target and against the gate's cap, and the distance to
each risk limit. A model never sees a raw table and never produces a figure: it is handed
this record and asked the one question arithmetic cannot answer.

Where a number cannot be computed it is ``None`` and :attr:`Holding.gaps` says which input
was missing. A missing number is never guessed and never defaulted to something
convenient — a watcher that invents a NAV would raise hands about weights that do not
exist.

Sources, all read-only:

* ``ft_userdata/<sleeve>/tradesv3.sqlite`` — Freqtrade's own trade ledger: the position,
  its entry, its live stop, its high-water and low-water marks since entry.
* ``knowledge/earn.db`` — the mark: the freshest of the latest order-book mid and the
  latest closed 1h candle, whichever is newer, with its age reported.
* ``config/earn.yaml`` — the caps, the stop and take-profit mechanics.
* the most recent proposal row — the target weight Claude asked for, and its plan.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.config import EarnConfig
from ops.lib import paths as earn_paths

__all__ = ["Holding", "Mark", "open_holdings", "mark_for", "nav_basis"]

_SLEEVES: tuple[str, ...] = ("a", "b")
_MS_HOUR = 3_600_000


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(f"{value:.{digits}g}")
    return value


@dataclass(frozen=True)
class Mark:
    """The price the watcher measures against, and how stale it is."""

    price: float | None
    source: str | None = None            # 'book_mid' | 'candle_1h_close'
    as_of_utc: str | None = None
    age_min: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"price": _round(self.price), "source": self.source,
                "as_of_utc": self.as_of_utc, "age_min": _round(self.age_min, 4)}


@dataclass
class Holding:
    """One open position, fully measured. Every field here was computed, not asked for."""

    sleeve: str
    pair: str
    base: str
    trade_id: int
    amount: float
    entry: float
    opened_utc: str
    mark: Mark

    # -- computed ---------------------------------------------------------------
    age_hours: float | None = None
    value_usdt: float | None = None
    pnl_pct: float | None = None
    pnl_usdt: float | None = None
    stop: float | None = None
    stop_pct_from_entry: float | None = None
    dist_to_stop_pct: float | None = None
    through_stop: bool = False
    take_profit: float | None = None
    take_profit_source: str | None = None
    dist_to_take_profit_pct: float | None = None
    through_take_profit: bool = False
    peak_since_entry: float | None = None
    trough_since_entry: float | None = None
    drawdown_from_peak_pct: float | None = None
    nav_usdt: float | None = None
    nav_basis: str | None = None
    weight_now: float | None = None
    target_weight: float | None = None
    weight_gap: float | None = None
    weight_cap: float | None = None
    weight_cap_source: str | None = None
    headroom_to_cap: float | None = None
    gross_exposure: float | None = None
    gross_cap: float | None = None
    usdt_share: float | None = None
    usdt_floor: float | None = None
    moves: dict[str, float | None] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    # -- presentation -----------------------------------------------------------

    @property
    def key(self) -> str:
        return f"{self.sleeve}:{self.pair}:{self.trade_id}"

    def as_dict(self) -> dict[str, Any]:
        """The flat, JSON-safe record. This is what gets stored and rendered."""
        out: dict[str, Any] = {
            "sleeve": self.sleeve, "pair": self.pair, "base": self.base,
            "trade_id": self.trade_id, "amount": _round(self.amount),
            "entry": _round(self.entry), "opened_utc": self.opened_utc,
            "mark": self.mark.as_dict(),
        }
        for name in ("age_hours", "value_usdt", "pnl_pct", "pnl_usdt", "stop",
                     "stop_pct_from_entry", "dist_to_stop_pct", "take_profit",
                     "dist_to_take_profit_pct", "peak_since_entry", "trough_since_entry",
                     "drawdown_from_peak_pct", "nav_usdt", "weight_now", "target_weight",
                     "weight_gap", "weight_cap", "headroom_to_cap", "gross_exposure",
                     "gross_cap", "usdt_share", "usdt_floor"):
            out[name] = _round(getattr(self, name))
        out["through_stop"] = self.through_stop
        out["through_take_profit"] = self.through_take_profit
        out["take_profit_source"] = self.take_profit_source
        out["weight_cap_source"] = self.weight_cap_source
        out["nav_basis"] = self.nav_basis
        out["moves"] = {k: _round(v) for k, v in self.moves.items()}
        out["gaps"] = list(self.gaps)
        return out

    def facts(self) -> dict[str, float | bool | None]:
        """The subset an invalidation condition may be evaluated against.

        Flat, named the way a human writes them, and containing nothing a model produced.
        """
        return {
            "price": self.mark.price,
            "entry": self.entry,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "pnl_pct": self.pnl_pct,
            "drawdown_pct": self.drawdown_from_peak_pct,
            "dist_to_stop_pct": self.dist_to_stop_pct,
            "weight": self.weight_now,
            "age_hours": self.age_hours,
            "through_stop": self.through_stop,
            "peak": self.peak_since_entry,
            "trough": self.trough_since_entry,
            **{f"ret_{k}": v for k, v in self.moves.items() if k.endswith(("h", "d"))},
            **{k: v for k, v in self.moves.items() if not k.endswith(("h", "d"))},
        }


# --------------------------------------------------------------------------- marks


def mark_for(kdb: sqlite3.Connection | None, pair: str,
             *, now: datetime | None = None) -> Mark:
    """The freshest usable price for ``pair``: order-book mid, or the last closed 1h candle.

    Both are read; the newer wins. The age is reported so the caller can refuse to act on
    a price nobody has refreshed in an hour — staleness is a fact, not an excuse.
    """
    now = now or datetime.now(UTC)
    if kdb is None:
        return Mark(price=None)
    best: tuple[datetime, float, str] | None = None
    try:
        row = kdb.execute(
            "SELECT mid, captured_at FROM book_snapshots WHERE pair = ?"
            " ORDER BY captured_at DESC LIMIT 1", (pair,)).fetchone()
    except sqlite3.Error:
        row = None
    if row and row["mid"] is not None:
        when = _parse_iso(row["captured_at"])
        if when is not None:
            best = (when, float(row["mid"]), "book_mid")
    # The newest candle that has actually CLOSED. A bar in progress carries a close_time
    # in the future, and taking that as its timestamp made a mark look newer than it was —
    # a 17:00 bar looked like 17:59 evidence at 17:25, which would beat a genuinely fresh
    # order-book snapshot and report an age of zero for a price that is 25 minutes stale.
    now_ms = int(now.timestamp() * 1000)
    try:
        crow = kdb.execute(
            "SELECT close, close_time, open_time FROM candles"
            " WHERE pair = ? AND tf = '1h' AND close IS NOT NULL"
            " AND COALESCE(close_time, open_time + ?) <= ?"
            " ORDER BY open_time DESC LIMIT 1", (pair, _MS_HOUR, now_ms)).fetchone()
    except sqlite3.Error:
        crow = None
    if crow and crow["close"] is not None:
        stamp = crow["close_time"] or (int(crow["open_time"]) + _MS_HOUR)
        when = datetime.fromtimestamp(int(stamp) / 1000, tz=UTC)
        if best is None or when > best[0]:
            best = (when, float(crow["close"]), "candle_1h_close")
    if best is None:
        return Mark(price=None)
    when, price, source = best
    return Mark(price=price, source=source, as_of_utc=_iso(when),
                age_min=max(0.0, (now - when).total_seconds() / 60.0))


def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _parse_ft_date(text: Any) -> datetime | None:
    """Freqtrade writes naive UTC ``YYYY-MM-DD HH:MM:SS.ffffff``."""
    if not text:
        return None
    raw = str(text).strip().replace("Z", "")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# --------------------------------------------------------------------------- NAV


def _ft_db(sleeve: str, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else earn_paths.state_root()
    return base / "ft_userdata" / sleeve / "tradesv3.sqlite"


def nav_basis(sleeve: str, root: Path | None = None) -> tuple[float | None, str]:
    """The sleeve's starting balance and where it came from.

    Order: the signed mode file (the only authority when real money is at stake), then the
    committed Freqtrade config's ``dry_run_wallet``. Unresolved means every weight this
    cycle is ``None`` — which is the correct answer, not a reason to guess one.
    """
    base = Path(root) if root is not None else earn_paths.state_root()
    try:
        from ops.lib import mode_state  # noqa: PLC0415 — optional, and its import may fail

        state = mode_state.load()
        if state.verified:
            seed = state.sleeve(sleeve).seed_usdt
            if seed:
                return float(seed), "mode_state.seed_usdt"
    except Exception:  # noqa: BLE001 — mode_state never raises, but its import might
        pass
    cfg_path = base / "config" / f"freqtrade-{sleeve}.json"
    try:
        wallet = json.loads(cfg_path.read_text(encoding="utf-8")).get("dry_run_wallet")
    except (OSError, json.JSONDecodeError, AttributeError):
        wallet = None
    if isinstance(wallet, (int, float)) and wallet > 0:
        return float(wallet), f"freqtrade-{sleeve}.json:dry_run_wallet"
    return None, "unresolved"


# --------------------------------------------------------------------------- moves


def _pair_moves(kdb: sqlite3.Connection | None, pair: str,
                mark: Mark) -> dict[str, float | None]:
    """A handful of notable changes, from candles only. Six numbers, not twenty-four.

    The watcher is not the scanner: it does not need the full feature set, it needs enough
    context for "is this move the ordinary kind". RSI comes from
    :func:`runs.signals.features.rsi` so there is exactly one definition of it in the repo.
    """
    out: dict[str, float | None] = {"1h": None, "4h": None, "24h": None,
                                    "rsi_4h": None, "vol_ann_20d": None,
                                    "range_24h_pct": None}
    if kdb is None or mark.price is None:
        return out
    closes_1h = _closes(kdb, pair, "1h", 30)
    if len(closes_1h) >= 2:
        out["1h"] = _pct(mark.price, closes_1h[-2])
    if len(closes_1h) >= 5:
        out["4h"] = _pct(mark.price, closes_1h[-5])
    if len(closes_1h) >= 25:
        out["24h"] = _pct(mark.price, closes_1h[-25])
        window = closes_1h[-25:]
        low, high = min(window), max(window)
        if low > 0:
            out["range_24h_pct"] = (high - low) / low * 100.0
    closes_4h = _closes(kdb, pair, "4h", 40)
    if len(closes_4h) >= 15:
        from runs.signals.features import rsi  # noqa: PLC0415 — one RSI in the repo

        out["rsi_4h"] = rsi(closes_4h, 14)
    closes_1d = _closes(kdb, pair, "1d", 25)
    if len(closes_1d) >= 6:
        rets = [math.log(b / a) for a, b in zip(closes_1d, closes_1d[1:], strict=False)
                if a > 0 and b > 0]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            out["vol_ann_20d"] = math.sqrt(var) * math.sqrt(365.0)
    return out


def _closes(kdb: sqlite3.Connection, pair: str, tf: str, limit: int) -> list[float]:
    try:
        rows = kdb.execute(
            "SELECT close FROM candles WHERE pair = ? AND tf = ? AND close IS NOT NULL"
            " ORDER BY open_time DESC LIMIT ?", (pair, tf, int(limit))).fetchall()
    except sqlite3.Error:
        return []
    return [float(r["close"]) for r in reversed(rows)]


def _pct(now: float, then: float) -> float | None:
    if not then:
        return None
    return (now - then) / then * 100.0


# --------------------------------------------------------------------------- targets


def _latest_targets(jdb: Any | None) -> tuple[dict[str, float], str | None]:
    """The most recent non-shadow proposal's target weights, and its run id."""
    if jdb is None:
        return {}, None
    try:
        row = jdb.execute(
            "SELECT run_id, targets_json FROM proposals WHERE shadow = 0 AND valid = 1"
            " ORDER BY ts_utc DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return {}, None
    if row is None:
        return {}, None
    try:
        targets = json.loads(row["targets_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}, None
    if not isinstance(targets, dict):
        return {}, None
    return ({str(k).upper(): float(v) for k, v in targets.items()
             if isinstance(v, (int, float))}, row["run_id"])


def _cap_for(cfg: EarnConfig, base: str) -> tuple[float | None, str]:
    explicit = dict(getattr(cfg.risk, "max_weight", {}) or {})
    if base in explicit:
        return float(explicit[base]), "risk.max_weight"
    return None, "no explicit cap (tier cap needs the universe snapshot)"


def _take_profit(cfg: EarnConfig, entry: float) -> tuple[float | None, str]:
    """The take-profit level the mechanics imply, or ``None`` when there is none.

    ``roi_table: {"0": 10.0}`` is the shipped "effectively off" setting; reporting a
    +1000% target as if it were a level would be arithmetically true and completely
    useless, so it is reported as off.
    """
    tp = cfg.trading.defaults.take_profit
    ladder = list(getattr(tp, "ladder", []) or [])
    if ladder:
        first = ladder[0]
        pct = float(getattr(first, "at_profit_pct", 0) or 0)
        if pct > 0:
            return entry * (1.0 + pct), f"trading.take_profit.ladder[0] +{pct:.1%}"
    roi = dict(getattr(tp, "roi_table", {}) or {})
    best: float | None = None
    for value in roi.values():
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < pct < 1.0 and (best is None or pct < best):
            best = pct
    if best is not None:
        return entry * (1.0 + best), f"trading.take_profit.roi_table +{best:.1%}"
    return None, "off (roi_table effectively disabled, no ladder)"


# --------------------------------------------------------------------------- the build


def open_holdings(cfg: EarnConfig, *, kdb: sqlite3.Connection | None = None,
                  jdb: Any | None = None, root: Path | None = None,
                  sleeves: tuple[str, ...] = _SLEEVES,
                  now: datetime | None = None) -> list[Holding]:
    """Every open position across the sleeves, fully measured.

    Reads Freqtrade's ledger directly and read-only. A sleeve whose database is absent
    (never started, or a fresh checkout) contributes nothing and is not an error.
    """
    now = now or datetime.now(UTC)
    targets, _ = _latest_targets(jdb)
    out: list[Holding] = []
    for sleeve in sleeves:
        db = _ft_db(sleeve, root)
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        conn.row_factory = sqlite3.Row
        try:
            out.extend(_holdings_for(cfg, conn, sleeve, kdb=kdb, targets=targets,
                                     root=root, now=now))
        finally:
            conn.close()
    return out


def _holdings_for(cfg: EarnConfig, ft: sqlite3.Connection, sleeve: str, *,
                  kdb: sqlite3.Connection | None, targets: dict[str, float],
                  root: Path | None, now: datetime) -> list[Holding]:
    try:
        rows = ft.execute(
            "SELECT id, pair, amount, open_rate, open_date, stop_loss, stop_loss_pct,"
            " max_rate, min_rate FROM trades WHERE is_open = 1").fetchall()
        closed = ft.execute(
            "SELECT COALESCE(SUM(close_profit_abs), 0) AS realised FROM trades"
            " WHERE is_open = 0").fetchone()
    except sqlite3.Error:
        return []
    if not rows:
        return []

    start, basis = nav_basis(sleeve, root)
    realised = float(closed["realised"] or 0.0) if closed else 0.0

    marks = {r["pair"]: mark_for(kdb, r["pair"], now=now) for r in rows}
    gross_value = 0.0
    cost_basis = 0.0
    priced = True
    for r in rows:
        mark = marks[r["pair"]]
        if mark.price is None:
            priced = False
            continue
        gross_value += float(r["amount"]) * mark.price
        cost_basis += float(r["amount"]) * float(r["open_rate"])
    nav: float | None = None
    if start is not None and priced:
        nav = start + realised + (gross_value - cost_basis)
    usdt_share = None
    if nav and nav > 0:
        usdt_share = max(0.0, (nav - gross_value)) / nav

    quote = cfg.universe.quote
    held: list[Holding] = []
    for r in rows:
        pair = str(r["pair"])
        base = pair.split("/")[0].upper()
        mark = marks[pair]
        entry = float(r["open_rate"])
        amount = float(r["amount"])
        opened = _parse_ft_date(r["open_date"])
        h = Holding(
            sleeve=sleeve, pair=pair, base=base, trade_id=int(r["id"]), amount=amount,
            entry=entry, opened_utc=_iso(opened) if opened else "", mark=mark,
        )
        if opened is None:
            h.gaps.append("open_date unreadable")
        else:
            h.age_hours = (now - opened).total_seconds() / 3600.0
        h.stop = float(r["stop_loss"]) if r["stop_loss"] is not None else None
        if h.stop is None:
            h.gaps.append("no stop recorded on the trade")
        elif entry:
            h.stop_pct_from_entry = (h.stop - entry) / entry * 100.0
        h.take_profit, h.take_profit_source = _take_profit(cfg, entry)
        h.peak_since_entry = float(r["max_rate"]) if r["max_rate"] is not None else None
        h.trough_since_entry = float(r["min_rate"]) if r["min_rate"] is not None else None

        if mark.price is None:
            h.gaps.append(f"no mark for {pair}: neither a book snapshot nor a 1h candle")
        else:
            price = mark.price
            h.value_usdt = amount * price
            h.pnl_pct = (price - entry) / entry * 100.0 if entry else None
            h.pnl_usdt = amount * (price - entry)
            if h.stop is not None and price:
                h.dist_to_stop_pct = (price - h.stop) / price * 100.0
                h.through_stop = price <= h.stop
            if h.take_profit is not None and price:
                h.dist_to_take_profit_pct = (h.take_profit - price) / price * 100.0
                h.through_take_profit = price >= h.take_profit
            if h.peak_since_entry:
                h.drawdown_from_peak_pct = (
                    (price - h.peak_since_entry) / h.peak_since_entry * 100.0)
            h.moves = _pair_moves(kdb, pair, mark)

        h.nav_usdt = nav
        h.nav_basis = basis
        if nav is None:
            h.gaps.append(f"NAV unresolved ({basis}); every weight is unknown")
        elif nav > 0 and h.value_usdt is not None:
            h.weight_now = h.value_usdt / nav
        h.target_weight = targets.get(base)
        if h.target_weight is None:
            h.gaps.append(f"no target weight for {base} in the latest valid proposal")
        elif h.weight_now is not None:
            h.weight_gap = h.weight_now - h.target_weight
        h.weight_cap, h.weight_cap_source = _cap_for(cfg, base)
        if h.weight_cap is not None and h.weight_now is not None:
            h.headroom_to_cap = h.weight_cap - h.weight_now
        if nav and nav > 0:
            h.gross_exposure = gross_value / nav
        h.gross_cap = float(cfg.risk.max_gross_exposure)
        h.usdt_share = usdt_share
        h.usdt_floor = float(cfg.risk.usdt_floor)
        if base == quote.upper():          # cash is a balance, not a position
            continue
        held.append(h)
    return held
