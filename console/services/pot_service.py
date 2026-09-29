"""The cumulative pot: what the money put in is worth, counted across every bot restart.

No FastAPI imports: unit-testable. Read-only everywhere: the bots' own databases are
opened ``mode=ro`` and the journal connection is the caller's.

Why this module exists — the ledger that reset
==============================================

On 2026-09-23 the two paper bots were restarted at 23:37Z onto the fast-test profile with
**fresh Freqtrade databases** (``ft_userdata/<s>/runs/test-<s>-000.sqlite``), leaving the
first evening's trades behind in ``ft_userdata/<s>/tradesv3.sqlite``. The console's ledger
(``nav_points``, written by ``runs/nav_tick.py``) computes

    nav = seed + profit_closed_coin (the bot's ``/profit``) + open unrealised

and ``/profit`` only ever sees the database the bot is *currently* running on. So at the
23:45Z tick ``profit_closed_coin`` went from −42.21 (sleeve a) and −27.56 (sleeve b) to
0.00, and both sleeves' NAV snapped back to the 10,000 seed. Nothing carried the earlier
realised loss forward: ``sleeve_runs`` has no row for either run (the restart was done by
hand, not through ``ops.modes`` which is the only writer of that table), so no run
boundary was recorded, ``nav_points.run_id`` is ``NULL`` on every row, and the seed the
tick adds to is a constant from config. That is the whole mechanism: **the ledger is keyed
on the bot's current database and nothing else knows there was a previous one.**
``journal.fills.run_id`` cannot rescue it either — the strategy stamps the run id from the
runtime file, so the first evening's fills carry ``test-a-000`` although they live in
``tradesv3.sqlite``. The bot databases themselves are the only record that cannot lie
about which run a trade belongs to, so this module reads them.

Three numbers, three definitions (paper-trading review §1.1)
============================================================

``cumulative_net_usdt``
    What the seed is worth now: ``seed + Σ realised (every closed trade in every run
    database, net of fees) + Σ unrealised (open positions marked to market)``. The one
    number that survives a restart.
``realised_current_run_usdt``
    Closed trades, net of fees, in the database the bot is running on now — what the
    old console card showed, minus the seed.
``open_mark_usdt``
    Mark-to-market P&L of the trades still open, from the bot's own ``/status`` when it is
    up, else the newest closed candle, else the last closed daily bar
    (``runs.features.trend.daily_closes``, feather ∪ knowledge DB). A position that cannot
    be priced is listed under ``unpriced`` and the total says it is partial, never 0.

The per-run table (``runs``) makes every boundary visible: one row per database, with
start, end, realised, fees and whether it is the one the bot is on now, so a restart can
never hide a number again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops import db
from ops.lib import paths as ops_paths

__all__ = [
    "DEFINITIONS",
    "FT_USERDATA_DIR",
    "LEGACY_DB",
    "RUNS_SUBDIR",
    "RunLedger",
    "annotate_fills",
    "fill_events",
    "panel",
    "read_run",
    "resolve_seed",
    "run_databases",
    "sleeve_pot",
]

#: Where the bots' databases live under the state root. There is no ``paths.*`` key for
#: this in ``config/earn.yaml`` (config is owned elsewhere), so it is a code constant —
#: the same literal ``ops/backup.py`` and ``runs/watch/positions.py`` use.
FT_USERDATA_DIR = "ft_userdata"
#: Freqtrade's default database: the 4-hour strategies wrote here until 2026-09-23 23:37Z.
LEGACY_DB = "tradesv3.sqlite"
#: Per-run databases (``ops.lib.paths.ft_run_db``): ``runs/<run_id>.sqlite``.
RUNS_SUBDIR = "runs"

#: How far an audit row may sit from a fill and still be the hand that caused it.
_ACTOR_WINDOW = timedelta(seconds=300)
#: How far back a ``targets:*`` gate refusal may sit from a ``target_zero`` exit it caused.
_CAUSE_WINDOW = timedelta(seconds=180)

#: The words the screen prints beside each number. Kept here, once, so the server and
#: the page never define them differently.
DEFINITIONS: dict[str, str] = {
    "cumulative_net_usdt": (
        "What the money put in is worth now: the seed, plus every closed trade in every "
        "run database net of fees, plus open positions marked to market. Counted across "
        "every bot restart."
    ),
    "realised_current_run_usdt": (
        "Closed trades, net of fees, since the bot started on its current database. This "
        "is the number that resets to zero on a restart."
    ),
    "open_mark_usdt": (
        "Profit or loss on the positions still open, at the newest price the system has. "
        "Not yet realised; it moves with the market."
    ),
}


# --------------------------------------------------------------------------- data classes


@dataclass
class OpenTrade:
    trade_id: int
    pair: str
    amount: float
    open_rate: float
    stake_amount: float
    fee_open: float
    fee_close: float
    open_date: str | None
    mark: float | None = None
    mark_source: str = "unpriced"
    unrealised: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "pair": self.pair,
            "amount": self.amount,
            "open_rate": self.open_rate,
            "stake_usdt": round(self.stake_amount, 8),
            "opened_utc": self.open_date,
            "mark": self.mark,
            "mark_source": self.mark_source,
            "unrealised_usdt": None if self.unrealised is None else round(self.unrealised, 8),
        }


@dataclass
class RunLedger:
    """One Freqtrade database: the trades in it and what they made."""

    key: str
    path: Path
    strategy: str | None = None
    started_utc: str | None = None
    ended_utc: str | None = None
    closed_trades: int = 0
    open: list[OpenTrade] = field(default_factory=list)
    realised_net: float = 0.0
    fees_closed: float = 0.0
    fees_open: float = 0.0
    #: ``orders.order_id`` -> the tag Freqtrade put on it (enter tag on a buy, exit
    #: reason on a sell), the trade it belongs to and when it filled.
    order_tags: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None
    mtime: float = 0.0
    current: bool = False

    @property
    def gross(self) -> float:
        return self.realised_net + self.fees_closed

    @property
    def fees(self) -> float:
        return self.fees_closed + self.fees_open

    def to_json(self, root: Path | None = None) -> dict[str, Any]:
        try:
            rel = str(self.path.relative_to(root)) if root else self.path.name
        except ValueError:
            rel = self.path.name
        return {
            "run": self.key,
            "db": rel.replace("\\", "/"),
            "strategy": self.strategy,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "current": self.current,
            "closed_trades": self.closed_trades,
            "open_trades": len(self.open),
            "realised_usdt": round(self.realised_net, 8),
            "fees_usdt": round(self.fees, 8),
            "gross_usdt": round(self.gross, 8),
            "error": self.error,
        }


# --------------------------------------------------------------------------- the databases


def _userdata(sleeve: str, root: Path | None) -> Path:
    base = Path(root) if root is not None else ops_paths.state_root()
    return base / FT_USERDATA_DIR / sleeve.lower()


def run_databases(sleeve: str, *, root: Path | None = None) -> list[Path]:
    """Every Freqtrade database this sleeve has ever written to, legacy file first."""
    base = _userdata(sleeve, root)
    out: list[Path] = []
    legacy = base / LEGACY_DB
    if legacy.exists():
        out.append(legacy)
    runs = base / RUNS_SUBDIR
    if runs.is_dir():
        out.extend(sorted(p for p in runs.glob("*.sqlite") if p.is_file()))
    return out


def _mtime(path: Path) -> float:
    """Newest of the file and its WAL — a WAL database's main file can be days old."""
    best = 0.0
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            best = max(best, candidate.stat().st_mtime)
        except OSError:
            continue
    return best


def _ft_utc(text: Any) -> str | None:
    """Freqtrade writes naive UTC ``YYYY-MM-DD HH:MM:SS.ffffff``; the journal wants ``Z``."""
    if not text:
        return None
    raw = str(text).strip().replace("Z", "")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_run(path: Path) -> RunLedger:
    """Read one Freqtrade database read-only. Never raises: a broken file is a row that
    says so, with zeros, rather than a blank pot."""
    led = RunLedger(key=path.stem, path=path, mtime=_mtime(path))
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        led.error = f"cannot open ({type(e).__name__})"
        return led
    conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute(
            "SELECT id, pair, is_open, open_date, close_date, open_rate, amount, stake_amount,"
            " fee_open, fee_close, fee_open_cost, fee_close_cost, close_profit_abs, strategy"
            " FROM trades ORDER BY id"
        ).fetchall()
        try:
            orders = conn.execute(
                "SELECT ft_trade_id, order_id, side, ft_order_side, status, filled, average,"
                " cost, ft_order_tag, order_filled_date FROM orders ORDER BY id"
            ).fetchall()
        except sqlite3.Error:
            orders = []
    except sqlite3.Error as e:
        led.error = f"cannot read trades ({type(e).__name__})"
        return led
    finally:
        conn.close()

    by_trade: dict[int, list[sqlite3.Row]] = {}
    for o in orders:
        by_trade.setdefault(int(o["ft_trade_id"]), []).append(o)
        oid = o["order_id"]
        if oid:
            led.order_tags[str(oid)] = {
                "trade_id": int(o["ft_trade_id"]),
                "side": str(o["side"] or o["ft_order_side"] or "").lower(),
                "tag": o["ft_order_tag"],
                "filled_utc": _ft_utc(o["order_filled_date"]),
                "run": led.key,
            }

    starts: list[str] = []
    ends: list[str] = []
    for t in trades:
        if led.strategy is None and t["strategy"]:
            led.strategy = str(t["strategy"])
        fee_open = _f(t["fee_open"])
        fee_close = _f(t["fee_close"])
        opened = _ft_utc(t["open_date"])
        if opened:
            starts.append(opened)
        fees_buy, fees_sell, filled_orders = 0.0, 0.0, 0
        for o in by_trade.get(int(t["id"]), []):
            if str(o["status"] or "") != "closed" or not _f(o["filled"]):
                continue
            filled_orders += 1
            cost = _f(o["cost"]) or _f(o["filled"]) * _f(o["average"])
            side = str(o["side"] or o["ft_order_side"] or "").lower()
            if side == "buy":
                fees_buy += cost * fee_open
            else:
                fees_sell += cost * fee_close
        if not filled_orders:
            # No orders table (older schema, or a test fixture): the trade row's own fee
            # columns are the best record left. ``fee_close_cost`` covers the LAST exit
            # order only, which understates partial exits — hence orders first.
            fees_buy = _f(t["fee_open_cost"])
            fees_sell = _f(t["fee_close_cost"]) if not int(t["is_open"] or 0) else 0.0
        if int(t["is_open"] or 0):
            led.open.append(OpenTrade(
                trade_id=int(t["id"]), pair=str(t["pair"]), amount=_f(t["amount"]),
                open_rate=_f(t["open_rate"]), stake_amount=_f(t["stake_amount"]),
                fee_open=fee_open, fee_close=fee_close, open_date=opened,
            ))
            led.fees_open += fees_buy + fees_sell
        else:
            led.closed_trades += 1
            led.realised_net += _f(t["close_profit_abs"])
            led.fees_closed += fees_buy + fees_sell
            closed = _ft_utc(t["close_date"])
            if closed:
                ends.append(closed)
    led.started_utc = min(starts) if starts else None
    led.ended_utc = max(ends) if ends and not led.open else None
    return led


# --------------------------------------------------------------------------- marks


def _mark_for(pair: str, marks: Mapping[str, float] | None, cfg: Any,
              root: Path | None) -> tuple[float | None, str]:
    """A price for ``pair``: the newest closed candle first, the last closed daily bar
    (feather ∪ knowledge DB, via ``runs.features.trend.daily_closes``) second."""
    asset = pair.split("/")[0].upper()
    if marks:
        price = marks.get(asset)
        if price:
            return float(price), "candle"
    try:
        from runs.features.trend import daily_closes

        kpath = Path(db.knowledge_path(cfg, root))
        data_dir = getattr(getattr(cfg, "paths", None), "data_dir", "data") or "data"
        data_root = (Path(root) / data_dir) if root is not None \
            else ops_paths.data_path(str(data_dir))
        kdb = None
        try:
            if kpath.exists():
                kdb = sqlite3.connect(f"file:{kpath.as_posix()}?mode=ro", uri=True)
                kdb.row_factory = sqlite3.Row
            closes, source = daily_closes(pair, kdb=kdb, data_root=data_root)
        finally:
            if kdb is not None:
                kdb.close()
        if len(closes):
            last = float(closes.iloc[-1])
            if last > 0:
                return last, f"daily:{source}"
    except Exception:  # noqa: BLE001 - a price that cannot be found is "unpriced", not a crash
        pass
    return None, "unpriced"


def _mark_open(led: RunLedger, cfg: Any, root: Path | None,
               bot_status: Sequence[Mapping[str, Any]] | None,
               marks: Mapping[str, float] | None) -> None:
    """Fill in ``mark``/``unrealised`` on every open trade of the current run."""
    by_id: dict[int, Mapping[str, Any]] = {}
    for row in bot_status or []:
        try:
            by_id[int(row.get("trade_id"))] = row
        except (TypeError, ValueError):
            continue
    for trade in led.open:
        live = by_id.get(trade.trade_id)
        if live is not None and live.get("profit_abs") is not None:
            trade.unrealised = _f(live.get("profit_abs"))
            trade.mark = _f(live.get("current_rate")) or None
            trade.mark_source = "bot"
            continue
        mark, source = _mark_for(trade.pair, marks, cfg, root)
        trade.mark, trade.mark_source = mark, source
        if mark is None:
            trade.unrealised = None
            continue
        # Freqtrade's own arithmetic: close value net of the exit fee minus open value
        # gross of the entry fee.
        close_value = trade.amount * mark * (1.0 - trade.fee_close)
        open_value = trade.amount * trade.open_rate * (1.0 + trade.fee_open)
        trade.unrealised = close_value - open_value


# --------------------------------------------------------------------------- the pot


def _active_run(conn: sqlite3.Connection | None, sleeve: str) -> dict[str, Any]:
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT run_id, seed_usdt, ft_db_path, started_utc FROM sleeve_runs"
            " WHERE sleeve=? AND status='active' ORDER BY started_utc DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    return dict(row) if row else {}


def resolve_seed(conn: sqlite3.Connection | None, cfg: Any, sleeve: str) -> tuple[
        float | None, str]:
    """``(seed, source)``: the active ``sleeve_runs`` row, else ``ops.config.seed_for``
    (the signed mode file when verified, the configured test seed otherwise)."""
    run = _active_run(conn, sleeve)
    if run.get("seed_usdt") is not None:
        return float(run["seed_usdt"]), "run"
    try:
        from ops.config import seed_for

        return float(seed_for(cfg, sleeve)), "config"
    except Exception:  # noqa: BLE001 - an unreadable seed is "not set", never a crash
        return None, "none"


def _ledger_nav(conn: sqlite3.Connection | None, sleeve: str) -> dict[str, Any]:
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT ts_utc, nav_usdt, realized_pnl FROM nav_points WHERE sleeve=?"
            " ORDER BY ts_utc DESC LIMIT 1", (sleeve,)).fetchone()
    except sqlite3.Error:
        return {}
    return dict(row) if row else {}


def _pick_current(runs: list[RunLedger], active: Mapping[str, Any]) -> None:
    if not runs:
        return
    wanted = str(active.get("ft_db_path") or "").rsplit("/", 1)[-1]
    if wanted:
        for led in runs:
            if led.path.name == wanted:
                led.current = True
                return
    newest = max(runs, key=lambda r: (r.mtime, r.started_utc or ""))
    newest.current = True


def sleeve_pot(cfg: Any, sleeve: str, *, conn: sqlite3.Connection | None = None,
               root: Path | None = None, seed_usdt: float | None = None,
               seed_source: str | None = None,
               bot_status: Sequence[Mapping[str, Any]] | None = None,
               marks: Mapping[str, float] | None = None) -> dict[str, Any]:
    """One sleeve's cumulative pot, its three numbers and its per-run table."""
    sleeve = sleeve.lower()
    if seed_usdt is None:
        seed_usdt, seed_source = resolve_seed(conn, cfg, sleeve)
    active = _active_run(conn, sleeve)
    runs = [read_run(p) for p in run_databases(sleeve, root=root)]
    runs.sort(key=lambda r: (r.started_utc or "9999", r.mtime))
    _pick_current(runs, active)
    current = next((r for r in runs if r.current), None)
    if current is not None:
        _mark_open(current, cfg, root, bot_status, marks)
    # Open trades in a database the bot has left behind are a data fault worth seeing,
    # not a position: they are listed, unpriced, and excluded from the total.
    realised_all = sum(r.realised_net for r in runs)
    fees_all = sum(r.fees for r in runs)
    gross_all = sum(r.gross for r in runs)
    open_rows = list(current.open) if current is not None else []
    unpriced = [t.pair for t in open_rows if t.unrealised is None]
    open_mark = sum(t.unrealised for t in open_rows if t.unrealised is not None)
    open_value = sum(t.amount * t.mark for t in open_rows if t.mark)
    cumulative = None if seed_usdt is None else seed_usdt + realised_all + open_mark
    ledger = _ledger_nav(conn, sleeve)
    ledger_nav = ledger.get("nav_usdt")
    return {
        "sleeve": sleeve,
        "seed_usdt": seed_usdt,
        "seed_source": seed_source or ("none" if seed_usdt is None else "config"),
        "cumulative_net_usdt": None if cumulative is None else round(cumulative, 8),
        "gain_usdt": None if cumulative is None else round(cumulative - seed_usdt, 8),
        "realised_all_runs_usdt": round(realised_all, 8),
        "realised_current_run_usdt": round(current.realised_net, 8) if current else 0.0,
        "realised_earlier_runs_usdt": round(
            realised_all - (current.realised_net if current else 0.0), 8),
        "open_mark_usdt": round(open_mark, 8),
        "open_value_usdt": round(open_value, 8),
        "fees_usdt": round(fees_all, 8),
        "gross_usdt": round(gross_all, 8),
        "fully_priced": not unpriced,
        "unpriced": unpriced,
        "runs": [r.to_json(root if root is not None else ops_paths.state_root())
                 for r in runs],
        "restarts": max(0, len(runs) - 1),
        "current_run": current.key if current else None,
        "open": [t.to_json() for t in open_rows],
        # The old ledger beside the truth, so the gap is a number on the screen and not
        # a memory: nav_points restarts from the seed whenever the bot gets a fresh DB.
        "ledger_nav_usdt": ledger_nav,
        "ledger_as_of_utc": ledger.get("ts_utc"),
        "ledger_gap_usdt": None if (cumulative is None or ledger_nav is None)
        else round(cumulative - float(ledger_nav), 8),
    }


def panel(cfg: Any, conn: sqlite3.Connection | None, seed_panel: Mapping[str, Any] | None,
          *, root: Path | None = None, marks: Mapping[str, float] | None = None,
          bot_status: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
          sleeves: Iterable[str] = ops_paths.SLEEVES) -> dict[str, Any]:
    """The Overview's ``pot`` block: both sleeves and, when they are on the same kind of
    money, the total. A mixed basis has no total, exactly as the seed panel has none."""
    seed_rows = {str(r.get("sleeve")): r for r in (seed_panel or {}).get("sleeves", [])}
    rows: list[dict[str, Any]] = []
    for sleeve in sleeves:
        seed_row = seed_rows.get(sleeve, {})
        rows.append(sleeve_pot(
            cfg, sleeve, conn=conn, root=root,
            seed_usdt=seed_row.get("seed_usdt"), seed_source=seed_row.get("source"),
            bot_status=(bot_status or {}).get(sleeve), marks=marks,
        ))
    mixed = bool((seed_panel or {}).get("mixed"))
    resolved = [r for r in rows if r["cumulative_net_usdt"] is not None]
    total: dict[str, Any] | None = None
    if resolved and not mixed:
        seed = sum(r["seed_usdt"] for r in resolved)
        cumulative = sum(r["cumulative_net_usdt"] for r in resolved)
        total = {
            "seed_usdt": round(seed, 8),
            "cumulative_net_usdt": round(cumulative, 8),
            "gain_usdt": round(cumulative - seed, 8),
            "gain_pct": round((cumulative - seed) / seed * 100.0, 4) if seed else None,
            "realised_all_runs_usdt": round(sum(r["realised_all_runs_usdt"] for r in resolved), 8),
            "realised_current_run_usdt": round(
                sum(r["realised_current_run_usdt"] for r in resolved), 8),
            "realised_earlier_runs_usdt": round(
                sum(r["realised_earlier_runs_usdt"] for r in resolved), 8),
            "open_mark_usdt": round(sum(r["open_mark_usdt"] for r in resolved), 8),
            "fees_usdt": round(sum(r["fees_usdt"] for r in resolved), 8),
            "gross_usdt": round(sum(r["gross_usdt"] for r in resolved), 8),
            "fully_priced": all(r["fully_priced"] for r in resolved),
            "unpriced": [p for r in resolved for p in r["unpriced"]],
            "restarts": sum(r["restarts"] for r in resolved),
            "runs": sum(len(r["runs"]) for r in resolved),
            "ledger_nav_usdt": (
                round(sum(float(r["ledger_nav_usdt"]) for r in resolved), 8)
                if all(r["ledger_nav_usdt"] is not None for r in resolved) else None),
        }
        if total["ledger_nav_usdt"] is not None:
            total["ledger_gap_usdt"] = round(cumulative - total["ledger_nav_usdt"], 8)
        else:
            total["ledger_gap_usdt"] = None
    return {
        "basis": (seed_panel or {}).get("basis", "simulated"),
        "mixed": mixed,
        "definitions": dict(DEFINITIONS),
        "total": total,
        "sleeves": rows,
        "as_of_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# --------------------------------------------------------------------------- who did what


def fill_events(sleeve: str, *, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """``fills.ft_order_id`` -> what the bot's own database says about that order."""
    out: dict[str, dict[str, Any]] = {}
    for path in run_databases(sleeve, root=root):
        out.update(read_run(path).order_tags)
    return out


def _parse_z(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _flatten_actor(conn: sqlite3.Connection | None, sleeve: str, ts: str) -> str | None:
    """The audit actor of a ``force_exit``: whoever typed the flatten or engaged the kill
    switch within five minutes of the fill. ``human:console:<sid>`` -> ``human:console``."""
    when = _parse_z(ts)
    if conn is None or when is None:
        return None
    lo = (when - _ACTOR_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (when + _ACTOR_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = conn.execute(
            "SELECT actor, ts_utc, target FROM audit_log WHERE result='ok'"
            " AND action IN ('autonomy.flatten', 'kill.engage', 'bot.forceexit')"
            " AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc", (lo, hi)).fetchall()
    except sqlite3.Error:
        return None
    best: tuple[float, str] | None = None
    for row in rows:
        target = str(row["target"] or "").lower()
        if target not in ("", sleeve, "both", "all"):
            continue
        at = _parse_z(row["ts_utc"])
        gap = abs((at - when).total_seconds()) if at else 1e9
        if best is None or gap < best[0]:
            best = (gap, str(row["actor"]))
    if best is None:
        return None
    parts = best[1].split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else best[1]


def _target_zero_cause(conn: sqlite3.Connection | None, sleeve: str, ts: str) -> str | None:
    """The ``targets:*`` refusal that preceded a ``target_zero`` exit — the mandate the
    bot found missing when it sold everything."""
    when = _parse_z(ts)
    if conn is None or when is None:
        return None
    lo = (when - _CAUSE_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (when + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        row = conn.execute(
            "SELECT reason FROM gate_decisions WHERE sleeve=? AND allowed=0"
            " AND reason LIKE 'targets:%' AND ts_utc BETWEEN ? AND ?"
            " ORDER BY ts_utc DESC LIMIT 1", (sleeve, lo, hi)).fetchone()
    except sqlite3.Error:
        return None
    return str(row["reason"]) if row else None


def annotate_fills(fills: list[dict[str, Any]], events: Mapping[str, Mapping[str, Any]],
                   conn: sqlite3.Connection | None, sleeve: str) -> list[dict[str, Any]]:
    """Add ``reason`` (Freqtrade's enter tag or exit reason), ``actor`` (``human:console``
    for a hand flatten, ``bot`` otherwise), ``cause`` and ``run`` to each fill row.

    The join is exact: ``fills.ft_order_id`` is the bot's ``orders.order_id``. A fill with
    no matching order (a database that was deleted, a bot that was never ours) keeps its
    row and gets ``None`` everywhere — never a guessed reason.
    """
    for row in fills:
        event = events.get(str(row.get("ft_order_id") or ""))
        reason = str(event["tag"]) if event and event.get("tag") else None
        row["reason"] = reason
        row["run"] = event.get("run") if event else None
        row["actor"] = None
        row["cause"] = None
        if not event:
            continue
        side = str(row.get("side") or event.get("side") or "").lower()
        if side != "sell":
            row["actor"] = "bot"
            continue
        if reason == "force_exit":
            row["actor"] = _flatten_actor(conn, sleeve, str(row.get("ts_utc"))) or "unknown"
            row["cause"] = "flatten"
        elif reason == "target_zero":
            row["actor"] = "bot"
            row["cause"] = _target_zero_cause(conn, sleeve, str(row.get("ts_utc")))
        else:
            row["actor"] = "bot"
    return fills
