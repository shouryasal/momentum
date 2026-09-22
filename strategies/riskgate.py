"""The deterministic risk gate. TIER 2 — human-only. STDLIB ONLY (runs inside the
freqtrade container; ops/ is not mounted there).

Pure logic: RiskGate takes a GateConfig plus injected providers (flags, staleness,
kill) and a StateStore, so every limit is unit-testable without freqtrade. The
freqtrade adapter (earn_base.py) wires the callbacks to this.

Fail-closed rules (canonical contracts #5/#6): an unreadable or stale flags file
blocks entries; missing market data blocks entries; the monthly lock is cleared only
by a human (ops-runbook), never by code.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

GULF = timezone(timedelta(hours=4))  # Asia/Dubai, no DST
_EPS = 1e-9

CHECK_ORDER = (
    "kill", "monthly_lock", "daily_lock", "blackout", "staleness",
    "trades_per_day", "min_notional", "weight_cap", "gross_cap", "usdt_floor",
)


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class GateConfig:
    sleeve: str                      # 'a' | 'b'
    pairs: tuple[str, ...]
    weight_caps: dict[str, float]    # PAIR-keyed, resolved from asset caps + default
    gross_cap: float
    usdt_floor: float
    daily_stop: float
    monthly_stop: float
    daily_lock_hours: int
    max_trades_per_day: int
    min_notional: float
    staleness_minutes: int
    stoploss_per_trade: float
    stoploss_guard_count: int
    stoploss_guard_window_h: int
    stoploss_guard_lock_h: int
    cooldown_candles: int
    cross_ticks_buffer: float
    rebalance_band: float
    dust_weight: float
    proposal_max_age_h: int
    proposal_sum_tolerance: float
    drift_to_a_after_h: int
    flags_path: str
    kill_path: str
    knowledge_db: str
    proposals_dir: str
    config_dir: str
    phase: str

    @classmethod
    def load(cls, path: str | Path | None = None, sleeve: str | None = None) -> GateConfig:
        p = Path(path or os.environ.get("EARN_RISKGATE", "config/riskgate.json"))
        raw = json.loads(p.read_text())
        risk, uni, ex = raw["risk"], raw["universe"], raw["execution"]
        cp = raw["container_paths"]
        default_cap = risk["max_weight"].get("default", 0.0)
        caps = {
            pair: risk["max_weight"].get(pair.split("/")[0], default_cap)
            for pair in uni["pairs"]
        }
        return cls(
            sleeve=(sleeve or os.environ.get("EARN_SLEEVE", "a")).lower(),
            pairs=tuple(uni["pairs"]),
            weight_caps=caps,
            gross_cap=risk["max_gross_exposure"],
            usdt_floor=risk["usdt_floor"],
            daily_stop=risk["daily_loss_stop"],
            monthly_stop=risk["monthly_loss_stop"],
            daily_lock_hours=risk["daily_stop_lock_hours"],
            max_trades_per_day=risk["max_trades_per_day"],
            min_notional=risk["min_notional_usdt"],
            staleness_minutes=risk["staleness_minutes"],
            stoploss_per_trade=risk["stoploss_per_trade"],
            stoploss_guard_count=risk["stoploss_guard"]["count"],
            stoploss_guard_window_h=risk["stoploss_guard"]["window_hours"],
            stoploss_guard_lock_h=risk["stoploss_guard"]["lock_hours"],
            cooldown_candles=risk["cooldown_candles"],
            cross_ticks_buffer=ex["cross_ticks_buffer"],
            rebalance_band=ex["rebalance_band"],
            dust_weight=ex["dust_weight"],
            proposal_max_age_h=raw["proposal"]["max_age_hours"],
            proposal_sum_tolerance=raw["proposal"]["sum_tolerance"],
            drift_to_a_after_h=raw["sleeve_b"]["drift_to_a_after_h"],
            flags_path=cp["flags_file"],
            kill_path=cp["kill_file"],
            knowledge_db=cp["knowledge_db"],
            proposals_dir=cp["proposals_dir"],
            config_dir=cp["config_dir"],
            phase=raw["phase"],
        )


# --------------------------------------------------------------------------- providers

def flags_blocked(flags_path: str | Path, pair: str, now: datetime) -> tuple[bool, str]:
    """Stdlib mirror of ops.lib.flags.entries_blocked — FAIL-CLOSED. Semantics must
    stay identical; tests/strategies/test_gate_blackout.py cross-checks both."""
    try:
        data = json.loads(Path(flags_path).read_text())
        updated = datetime.fromisoformat(str(data["updated_at"]).replace("Z", "+00:00"))
        flags = data["flags"]
    except Exception:
        return True, "flags_unreadable"
    if now - updated > timedelta(hours=24):
        return True, "flags_stale"
    for name, f in flags.items():
        try:
            if not f.get("active"):
                continue
            exp = f.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) <= now:
                        continue
                except ValueError:
                    continue
            if f.get("severity") == "block_entries" and f.get("scope", "ALL") in ("ALL", pair):
                return True, name
        except AttributeError:
            return True, "flags_unreadable"
    return False, ""


def data_age_minutes(knowledge_db: str | Path, now: datetime) -> float:
    """Freshness of the ingest pipeline: max(book-snapshot age, 1h-candle age - 60min).

    Books arrive every 15 min, so >30 min means ingest is down. A 1h candle's open_time
    is legitimately up to 60 min old, hence the -60 allowance. Missing DB or rows ->
    +inf (fail-closed: no entries until data flows).
    """
    try:
        conn = sqlite3.connect(f"file:{knowledge_db}?mode=ro", uri=True)
        try:
            book = conn.execute("SELECT MAX(captured_at) FROM book_snapshots").fetchone()[0]
            candle = conn.execute(
                "SELECT MAX(open_time) FROM candles WHERE tf='1h'"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return float("inf")
    if book is None or candle is None:
        return float("inf")
    try:
        book_age = (now - datetime.fromisoformat(book.replace("Z", "+00:00"))).total_seconds() / 60
        candle_dt = datetime.fromtimestamp(candle / 1000, tz=UTC)
    except (ValueError, TypeError, OSError):
        return float("inf")
    candle_age = (now - candle_dt).total_seconds() / 60 - 60
    return max(book_age, candle_age, 0.0)


# --------------------------------------------------------------------------- state

class StateStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value


class SqliteStateStore:
    """risk_state table in journal.db. Failures degrade to in-memory (never break the loop)."""

    def __init__(self, db_path: str | Path, sleeve: str):
        self.db_path = str(db_path)
        self.sleeve = sleeve.lower()
        self._fallback = MemoryStateStore()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(self, key: str) -> str | None:
        try:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT value FROM risk_state WHERE sleeve=? AND key=?",
                    (self.sleeve, key),
                ).fetchone()
                return row[0] if row else self._fallback.get(key)
            finally:
                conn.close()
        except sqlite3.Error:
            return self._fallback.get(key)

    def set(self, key: str, value: str) -> None:
        self._fallback.set(key, value)
        try:
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
                        " VALUES (?,?,?,?)"
                        " ON CONFLICT(sleeve, key) DO UPDATE SET value=excluded.value,"
                        " updated_utc=excluded.updated_utc",
                        (self.sleeve, key, value,
                         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------- decisions

@dataclass
class PortfolioState:
    nav: float
    free_usdt: float
    positions: dict[str, float]        # pair -> position value in USDT
    now: datetime                      # tz-aware UTC


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str                        # 'ok' or the first failing check, e.g. 'weight_cap:BTC/USDT'
    checks: dict[str, bool]
    capped_stake: float | None = None


@dataclass(frozen=True)
class LoopActions:
    flatten: bool = False
    flatten_reason: str = ""
    lock_until: datetime | None = None
    monthly_lock: bool = False


def _gulf_date(now: datetime) -> str:
    return now.astimezone(GULF).strftime("%Y-%m-%d")


def _gulf_month(now: datetime) -> str:
    return now.astimezone(GULF).strftime("%Y-%m")


class RiskGate:
    """Pure enforcement. Providers are injected; backtests stub them out."""

    def __init__(self, cfg: GateConfig, store: StateStore, *,
                 flags_provider=None, staleness_provider=None, kill_provider=None):
        self.cfg = cfg
        self.store = store
        self._flags = flags_provider or (
            lambda pair, now: flags_blocked(cfg.flags_path, pair, now))
        self._age = staleness_provider or (
            lambda now: data_age_minutes(cfg.knowledge_db, now))
        self._kill = kill_provider or (lambda: Path(cfg.kill_path).exists())

    # -- entry -----------------------------------------------------------------

    def check_entry(self, pair: str, stake: float, ps: PortfolioState) -> GateDecision:
        checks: dict[str, bool] = {}
        reason = "ok"

        checks["kill"] = not self._kill()
        checks["monthly_lock"] = self.store.get("monthly_locked") != "1"
        checks["daily_lock"] = not self._daily_locked(ps.now)
        blocked, flag = self._flags(pair, ps.now)
        checks["blackout"] = not blocked
        checks["staleness"] = self._age(ps.now) <= self.cfg.staleness_minutes
        checks["trades_per_day"] = self.trades_today(ps.now) < self.cfg.max_trades_per_day
        checks["min_notional"] = stake >= self.cfg.min_notional
        nav = max(ps.nav, _EPS)
        pos = ps.positions.get(pair, 0.0)
        checks["weight_cap"] = (pos + stake) / nav <= self.cfg.weight_caps.get(pair, 0.0) + _EPS
        gross = sum(ps.positions.values())
        checks["gross_cap"] = (gross + stake) / nav <= self.cfg.gross_cap + _EPS
        checks["usdt_floor"] = ps.free_usdt - stake >= self.cfg.usdt_floor * nav - _EPS

        for name in CHECK_ORDER:
            if not checks[name]:
                qualifier = {
                    "blackout": flag, "weight_cap": pair, "gross_cap": pair,
                }.get(name)
                reason = f"{name}:{qualifier}" if qualifier else name
                break
        return GateDecision(allowed=(reason == "ok"), reason=reason, checks=checks)

    def cap_stake(self, pair: str, proposed: float, ps: PortfolioState) -> float:
        """Shrink a proposed stake to the tightest headroom; below min_notional -> 0."""
        nav = max(ps.nav, _EPS)
        pos = ps.positions.get(pair, 0.0)
        gross = sum(ps.positions.values())
        headrooms = (
            self.cfg.weight_caps.get(pair, 0.0) * nav - pos,
            self.cfg.gross_cap * nav - gross,
            ps.free_usdt - self.cfg.usdt_floor * nav,
            proposed,
        )
        stake = max(min(headrooms), 0.0)
        return stake if stake >= self.cfg.min_notional else 0.0

    # -- exit ------------------------------------------------------------------

    def check_exit(self, pair: str, exit_reason: str, ps: PortfolioState) -> GateDecision:
        # Exits reduce risk: always allowed (including under KILL — flattening must work).
        return GateDecision(True, "ok", {"exit_always_allowed": True})

    # -- loop / stops ----------------------------------------------------------

    def loop_tick(self, ps: PortfolioState) -> LoopActions:
        now = ps.now
        today, month = _gulf_date(now), _gulf_month(now)

        if self.store.get("day_anchor_date") != today:
            self.store.set("day_anchor_date", today)
            self.store.set("day_anchor_nav", repr(ps.nav))
            self.store.set("trades_today", "0")
            self.store.set("trades_today_date", today)
        if self.store.get("month_anchor_month") != month:
            self.store.set("month_anchor_month", month)
            self.store.set("month_anchor_nav", repr(ps.nav))

        day_anchor = float(self.store.get("day_anchor_nav") or ps.nav)
        month_anchor = float(self.store.get("month_anchor_nav") or ps.nav)
        day_ret = ps.nav / day_anchor - 1 if day_anchor > 0 else 0.0
        month_ret = ps.nav / month_anchor - 1 if month_anchor > 0 else 0.0

        if month_ret <= -self.cfg.monthly_stop and self.store.get("monthly_locked") != "1":
            self.store.set("monthly_locked", "1")
            self.store.set("monthly_locked_month", month)
            return LoopActions(flatten=True, flatten_reason="risk_stop_monthly", monthly_lock=True)

        if day_ret <= -self.cfg.daily_stop and self.store.get("daily_stop_fired_date") != today:
            lock_until = now + timedelta(hours=self.cfg.daily_lock_hours)
            self.store.set("daily_stop_fired_date", today)
            self.store.set("locked_until", lock_until.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return LoopActions(flatten=True, flatten_reason="risk_stop_daily",
                               lock_until=lock_until)
        return LoopActions()

    def _daily_locked(self, now: datetime) -> bool:
        raw = self.store.get("locked_until")
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        return now < until

    def flatten_pending(self) -> str | None:
        """Non-empty while a stop flatten is in force (drives custom_exit)."""
        if self.store.get("monthly_locked") == "1":
            return "risk_stop_monthly"
        raw = self.store.get("daily_stop_fired_date")
        if raw and self._daily_locked(datetime.now(UTC)):
            return "risk_stop_daily"
        return None

    # -- counters --------------------------------------------------------------

    def trades_today(self, now: datetime) -> int:
        if self.store.get("trades_today_date") != _gulf_date(now):
            return 0
        return int(self.store.get("trades_today") or 0)

    def record_entry_fill(self, now: datetime) -> None:
        today = _gulf_date(now)
        if self.store.get("trades_today_date") != today:
            self.store.set("trades_today_date", today)
            self.store.set("trades_today", "1")
        else:
            self.store.set("trades_today", str(int(self.store.get("trades_today") or 0) + 1))
