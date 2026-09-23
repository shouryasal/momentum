"""The deterministic risk gate. TIER 2 — human-only. STDLIB ONLY (runs inside the
freqtrade container; ops/ is not mounted there).

Pure logic: RiskGate takes a GateConfig plus injected providers (flags, staleness,
kill) and a StateStore, so every limit is unit-testable without freqtrade. The
freqtrade adapter (earn_base.py) wires the callbacks to this.

Fail-closed rules (canonical contracts #5/#6): an unreadable or stale flags file
blocks entries; missing freshness data blocks entries; a NAV the adapter could not
compute blocks entries; the monthly lock is cleared only by a human
(``human_resume_monthly`` / ``ops.lib.risk_resume``), never by the loop.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    from strategies.mechanics import deep_merge, is_risk_exit
except ImportError:  # in-container flat layout
    from mechanics import deep_merge, is_risk_exit

GULF = timezone(timedelta(hours=4))  # Asia/Dubai, no DST
_EPS = 1e-9

CHECK_ORDER = (
    "nav_valid", "kill", "monthly_lock", "daily_lock", "blackout", "staleness",
    "reconcile", "trades_per_day", "orders_per_day", "turnover_day", "fee_budget",
    "min_notional", "order_notional", "entries_per_trade", "weight_cap", "gross_cap",
    "usdt_floor",
)

# The subset that also gates a *discretionary* exit (a trim or a TP rung). Risk exits
# are never blocked — see mechanics.is_risk_exit.
EXIT_CHECK_ORDER = ("orders_per_day", "turnover_day", "fee_budget")

# The flag name reconciliation raises; it maps to its own check, not to 'blackout'.
RECONCILE_FLAG = "reconcile_mismatch"


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
    # --- mechanics / churn limits (spec sections 3 and 9) -------------------------
    max_entries_per_trade: int = 4
    max_order_notional_pct: float = 0.20
    max_orders_per_day: int = 12
    max_turnover_pct_per_day: float = 0.50
    max_fee_pct_per_month: float = 0.01
    market_entries_allowed: bool = False
    protection_drawdown_lookback: int = 6
    protection_drawdown_trade_limit: int = 2
    reconcile_tolerance_pct: float = 0.005
    reconcile_dust_usdt: float = 10.0
    reconcile_block_on_mismatch: bool = True
    entry_unfilled_timeout_min: int = 20
    exit_unfilled_timeout_min: int = 20
    exit_timeout_count: int = 3
    timeframe: str = "4h"
    startup_candles: int = 1320
    trading: dict[str, Any] = field(default_factory=dict)      # resolved for THIS sleeve
    plan_bounds: dict[str, Any] = field(default_factory=dict)
    bounds: dict[str, Any] = field(default_factory=dict)
    freshness_path: str = ""
    approvals_dir: str = ""
    # --- runtime (var/runtime/runtime-<s>.json, merged at load) -------------------
    mode: str = "test"               # 'test' | 'live'
    state: str = "TEST"
    submode: str | None = None
    run_id: str = ""
    seed_usdt: float = 0.0
    require_approval: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @classmethod
    def load(cls, path: str | Path | None = None, sleeve: str | None = None,
             runtime_path: str | Path | None = None) -> GateConfig:
        """Committed ``riskgate.json`` merged with this machine's ``runtime-<s>.json``.

        The runtime file (``$EARN_RUNTIME``) is the only thing that can say "live"; it
        is rendered from a signed mode state. Its absence or corruption leaves the
        committed TEST baseline in place — fail closed by construction.
        """
        p = Path(path or os.environ.get("EARN_RISKGATE", "config/riskgate.json"))
        raw = json.loads(p.read_text())
        risk, uni, ex = raw["risk"], raw["universe"], raw["execution"]
        cp = raw["container_paths"]
        sleeve_id = (sleeve or os.environ.get("EARN_SLEEVE", "a")).lower()
        default_cap = risk["max_weight"].get("default", 0.0)
        caps = {
            pair: risk["max_weight"].get(pair.split("/")[0], default_cap)
            for pair in uni["pairs"]
        }
        trading_all = raw.get("trading") or {}
        trading = dict((trading_all.get("sleeves") or {}).get(sleeve_id) or {})
        runtime = _load_runtime(runtime_path, sleeve_id)
        protections = (risk.get("protections") or {}).get("max_drawdown") or {}
        reconcile = risk.get("reconcile") or {}
        return cls(
            sleeve=sleeve_id,
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
            phase=runtime.get("mode_phase") or raw["phase"],
            max_entries_per_trade=int(risk.get("max_entries_per_trade", 4)),
            max_order_notional_pct=float(risk.get("max_order_notional_pct", 0.20)),
            max_orders_per_day=int(risk.get("max_orders_per_day", 12)),
            max_turnover_pct_per_day=float(risk.get("max_turnover_pct_per_day", 0.50)),
            max_fee_pct_per_month=float(risk.get("max_fee_pct_per_month", 0.01)),
            market_entries_allowed=bool(risk.get("market_entries_allowed", False)),
            protection_drawdown_lookback=int(protections.get("lookback_candles", 6)),
            protection_drawdown_trade_limit=int(protections.get("trade_limit", 2)),
            reconcile_tolerance_pct=float(reconcile.get("tolerance_pct", 0.005)),
            reconcile_dust_usdt=float(reconcile.get("dust_usdt", 10.0)),
            reconcile_block_on_mismatch=bool(reconcile.get("block_on_mismatch", True)),
            entry_unfilled_timeout_min=int(ex.get("entry_unfilled_timeout_min", 20)),
            exit_unfilled_timeout_min=int(ex.get("exit_unfilled_timeout_min", 20)),
            exit_timeout_count=int(ex.get("exit_timeout_count", 3)),
            timeframe=str(trading_all.get("timeframe", "4h")),
            startup_candles=int(trading_all.get("startup_candles", 1320) or 1320),
            trading=trading,
            plan_bounds=dict(trading_all.get("plan_bounds") or {}),
            bounds=dict(raw.get("bounds") or {}),
            freshness_path=str(cp.get("freshness_file") or _default_freshness(cp["knowledge_db"])),
            approvals_dir=str(runtime.get("approval_dir")
                              or Path(cp["proposals_dir"]) / "approved"),
            mode=str(runtime.get("mode", "test")),
            state=str(runtime.get("state", "TEST")),
            submode=runtime.get("submode"),
            run_id=str(runtime.get("run_id") or ""),
            seed_usdt=float(runtime.get("seed_usdt") or 0.0),
            require_approval=bool(runtime.get("require_approval", False)),
        )

    def mechanics(self, path: str, default: Any = None) -> Any:
        """One resolved mechanics value, e.g. ``cfg.mechanics('dca.step_pct')``."""
        node: Any = self.trading
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _default_freshness(knowledge_db: str) -> str:
    return str(Path(knowledge_db).parent / "state" / "freshness.json")


def _load_runtime(runtime_path: str | Path | None, sleeve: str) -> dict[str, Any]:
    """``var/runtime/runtime-<s>.json``. Anything unreadable leaves the TEST baseline."""
    raw = runtime_path or os.environ.get("EARN_RUNTIME")
    if not raw:
        return {}
    try:
        data = json.loads(Path(raw).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("sleeve", sleeve)).lower() != sleeve:
        return {}  # someone handed us the other sleeve's file: ignore it
    if str(data.get("mode", "test")) == "live":
        data = deep_merge(data, {"mode_phase": "live_" + str(data.get("submode") or "propose")})
    return data


# --------------------------------------------------------------------------- providers

def flags_blocked(flags_path: str | Path, pair: str, now: datetime) -> tuple[bool, str]:
    """Stdlib mirror of ops.lib.flags.entries_blocked — FAIL-CLOSED. Semantics must
    stay identical; tests/strategies/test_gate_guards.py cross-checks both."""
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


def data_age_minutes(freshness_path: str | Path, now: datetime) -> float:
    """Ingest freshness from ``knowledge/state/freshness.json`` — stdlib JSON, no SQLite.

    The knowledge DB is mounted read-only into the container while it is in WAL mode,
    so a SQLite read from here can fail and block every entry (verified HIGH #12).
    ``ops/lib/freshness.py`` (P1) writes this file atomically after each ingest phase:

    ```json
    {"version": 1, "updated_at": "2026-09-22T08:02:00Z",
     "sources": {"book_snapshots": "2026-09-22T07:55:00Z",
                 "candles_1h":     "2026-09-22T07:00:00Z"}}
    ```

    A source value may also be an object carrying ``latest_utc``. Age is
    ``max(book age, 1h-candle age - 60min, 0)`` — a 1h candle's open_time is
    legitimately up to 60 minutes old. Missing, unparseable or empty ⇒ ``+inf``
    (fail-closed: no entries until data flows).
    """
    try:
        data = json.loads(Path(freshness_path).read_text())
        sources = data["sources"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return float("inf")
    if not isinstance(sources, dict) or not sources:
        return float("inf")
    ages: list[float] = []
    for name, value in sources.items():
        ts = value.get("latest_utc") if isinstance(value, dict) else value
        try:
            seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return float("inf")
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        age = (now - seen).total_seconds() / 60.0
        if name.startswith("candles_"):
            age -= _tf_minutes(name.split("_", 1)[1])
        ages.append(age)
    if not ages:
        return float("inf")
    return max(max(ages), 0.0)


def _tf_minutes(tf: str) -> float:
    tf = (tf or "").strip().lower()
    if not tf:
        return 0.0
    try:
        n = float(tf[:-1])
    except ValueError:
        return 0.0
    return n * {"m": 1.0, "h": 60.0, "d": 1440.0}.get(tf[-1], 0.0)


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


class NamespacedStateStore:
    """Prefixes every key with ``run:<run_id>:`` so a test run starts from clean anchors.

    An empty run id is a pass-through, which keeps the committed TEST baseline and
    every existing unit test on the un-namespaced keys.
    """

    def __init__(self, inner: StateStore, run_id: str | None):
        self.inner = inner
        self.prefix = f"run:{run_id}:" if run_id else ""

    def get(self, key: str) -> str | None:
        return self.inner.get(self.prefix + key)

    def set(self, key: str, value: str) -> None:
        self.inner.set(self.prefix + key, value)


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
    """The bot's own ledger, never the exchange account (verified HIGH #11).

    ``nav = ledger_cash + reserved_usdt + Σ positions`` where ``ledger_cash`` already
    had every open trade's stake deducted — so USDT sitting in a resting entry order
    is counted exactly once. ``valid=False`` means the adapter could not build this
    from freqtrade's objects; the ``nav_valid`` check then blocks every entry.
    """

    nav: float
    free_usdt: float
    positions: dict[str, float]        # pair -> position value in USDT
    now: datetime                      # tz-aware UTC
    valid: bool = True
    reason: str = ""                   # why not, when valid is False
    ledger_cash: float = 0.0
    reserved_usdt: float = 0.0
    entries_used: dict[str, int] = field(default_factory=dict)   # pair -> filled entries

    @property
    def gross(self) -> float:
        return sum(self.positions.values())

    @property
    def gross_exposure(self) -> float:
        return self.gross / max(self.nav, _EPS)


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


@dataclass(frozen=True)
class ResumeResult:
    """What :meth:`RiskGate.human_resume_monthly` re-anchored."""

    resumed: bool
    anchor_nav: float
    anchor_month: str
    resumed_utc: str
    reason: str = ""


def _gulf_date(now: datetime) -> str:
    return now.astimezone(GULF).strftime("%Y-%m-%d")


def _gulf_month(now: datetime) -> str:
    return now.astimezone(GULF).strftime("%Y-%m")


def _fnum(raw: str | None, default: float = 0.0) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class RiskGate:
    """Pure enforcement. Providers are injected; backtests stub them out."""

    def __init__(self, cfg: GateConfig, store: StateStore, *,
                 flags_provider=None, staleness_provider=None, kill_provider=None):
        self.cfg = cfg
        self.store = NamespacedStateStore(store, cfg.run_id) if cfg.run_id else store
        self._flags = flags_provider or (
            lambda pair, now: flags_blocked(cfg.flags_path, pair, now))
        self._age = staleness_provider or (
            lambda now: data_age_minutes(cfg.freshness_path, now))
        self._kill = kill_provider or (lambda: Path(cfg.kill_path).exists())

    # -- entry -----------------------------------------------------------------

    def check_entry(self, pair: str, stake: float, ps: PortfolioState) -> GateDecision:
        checks: dict[str, bool] = {}
        reason = "ok"
        cfg = self.cfg

        checks["nav_valid"] = bool(ps.valid) and ps.nav > 0
        checks["kill"] = not self._kill()
        checks["monthly_lock"] = self.store.get("monthly_locked") != "1"
        checks["daily_lock"] = not self._daily_locked(ps.now)
        blocked, flag = self._flags(pair, ps.now)
        reconcile_hit = blocked and flag == RECONCILE_FLAG
        checks["blackout"] = not (blocked and not reconcile_hit)
        checks["staleness"] = self._age(ps.now) <= cfg.staleness_minutes
        checks["reconcile"] = not (reconcile_hit and cfg.reconcile_block_on_mismatch)
        checks["trades_per_day"] = self.trades_today(ps.now) < cfg.max_trades_per_day
        checks["orders_per_day"] = self.orders_today(ps.now) < cfg.max_orders_per_day
        nav = max(ps.nav, _EPS)
        checks["turnover_day"] = (
            (self.turnover_today(ps.now) + stake) / nav
            <= cfg.max_turnover_pct_per_day + _EPS
        )
        checks["fee_budget"] = (
            self.fees_this_month(ps.now) / nav <= cfg.max_fee_pct_per_month + _EPS
        )
        checks["min_notional"] = stake >= cfg.min_notional
        checks["order_notional"] = stake / nav <= cfg.max_order_notional_pct + _EPS
        checks["entries_per_trade"] = (
            ps.entries_used.get(pair, 0) < cfg.max_entries_per_trade
        )
        pos = ps.positions.get(pair, 0.0)
        checks["weight_cap"] = (pos + stake) / nav <= cfg.weight_caps.get(pair, 0.0) + _EPS
        gross = ps.gross
        checks["gross_cap"] = (gross + stake) / nav <= cfg.gross_cap + _EPS
        checks["usdt_floor"] = ps.free_usdt - stake >= cfg.usdt_floor * nav - _EPS

        for name in CHECK_ORDER:
            if not checks[name]:
                qualifier = {
                    "blackout": flag, "weight_cap": pair, "gross_cap": pair,
                    "entries_per_trade": pair, "nav_valid": ps.reason or None,
                }.get(name)
                reason = f"{name}:{qualifier}" if qualifier else name
                break
        return GateDecision(allowed=(reason == "ok"), reason=reason, checks=checks)

    def cap_stake(self, pair: str, proposed: float, ps: PortfolioState) -> float:
        """Shrink a proposed stake to the tightest headroom; below min_notional -> 0."""
        if not ps.valid:
            return 0.0
        cfg = self.cfg
        nav = max(ps.nav, _EPS)
        pos = ps.positions.get(pair, 0.0)
        headrooms = (
            cfg.weight_caps.get(pair, 0.0) * nav - pos,
            cfg.gross_cap * nav - ps.gross,
            ps.free_usdt - cfg.usdt_floor * nav,
            cfg.max_order_notional_pct * nav,
            max(cfg.max_turnover_pct_per_day * nav - self.turnover_today(ps.now), 0.0),
            proposed,
        )
        stake = max(min(headrooms), 0.0)
        return stake if stake >= cfg.min_notional else 0.0

    # -- exit ------------------------------------------------------------------

    def check_exit(self, pair: str, exit_reason: str, ps: PortfolioState) -> GateDecision:
        # Exits reduce risk: always allowed (including under KILL — flattening must work).
        return GateDecision(True, "ok", {"exit_always_allowed": True})

    def check_discretionary_exit(self, pair: str, stake: float, ps: PortfolioState,
                                 exit_reason: str) -> GateDecision:
        """Churn/turnover/fee gate for a *discretionary* exit (TP rung, rebalance trim).

        A risk exit — stop, trailing stop, daily/monthly flatten, KILL, ``target_zero``
        — is never blocked: the answer is always ``allowed``.
        """
        if is_risk_exit(exit_reason):
            return GateDecision(True, "ok", {"risk_exit": True})
        cfg = self.cfg
        nav = max(ps.nav, _EPS)
        checks = {
            "orders_per_day": self.orders_today(ps.now) < cfg.max_orders_per_day,
            "turnover_day": ((self.turnover_today(ps.now) + abs(stake)) / nav
                             <= cfg.max_turnover_pct_per_day + _EPS),
            "fee_budget": (self.fees_this_month(ps.now) / nav
                           <= cfg.max_fee_pct_per_month + _EPS),
        }
        reason = "ok"
        for name in EXIT_CHECK_ORDER:
            if not checks[name]:
                reason = name
                break
        return GateDecision(allowed=(reason == "ok"), reason=reason, checks=checks)

    # -- loop / stops ----------------------------------------------------------

    def loop_tick(self, ps: PortfolioState) -> LoopActions:
        now = ps.now
        today, month = _gulf_date(now), _gulf_month(now)
        if not ps.valid:
            # No trustworthy NAV: do not move anchors and do not fire a stop off noise.
            return LoopActions()

        if self.store.get("day_anchor_date") != today:
            self.store.set("day_anchor_date", today)
            self.store.set("day_anchor_nav", repr(ps.nav))
            self.store.set("trades_today", "0")
            self.store.set("trades_today_date", today)
        if self.store.get("month_anchor_month") != month:
            self.store.set("month_anchor_month", month)
            self.store.set("month_anchor_nav", repr(ps.nav))

        day_anchor = _fnum(self.store.get("day_anchor_nav"), ps.nav)
        month_anchor = _fnum(self.store.get("month_anchor_nav"), ps.nav)
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

    def monthly_locked(self) -> bool:
        return self.store.get("monthly_locked") == "1"

    # -- human-only resume -----------------------------------------------------

    def human_resume_monthly(self, ps: PortfolioState) -> ResumeResult:
        """Clear the monthly stop and RE-ANCHOR the month to today's NAV (HIGH #10).

        Without the re-anchor the very next ``loop_tick`` compares the reduced NAV with
        the pre-drawdown anchor and re-locks instantly, so a resume never held. After
        this, only a *fresh* ``-monthly_stop`` measured from the new anchor re-arms.
        This is a human path: ``ops.lib.risk_resume.resume()`` is its only caller
        outside tests, and it needs a human actor.
        """
        if not ps.valid:
            return ResumeResult(False, 0.0, "", "", reason="nav_invalid")
        month = _gulf_month(ps.now)
        stamp = ps.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.set("month_anchor_month", month)
        self.store.set("month_anchor_nav", repr(ps.nav))
        self.store.set("monthly_locked", "0")
        self.store.set("monthly_locked_month", "")
        self.store.set("monthly_resumed_utc", stamp)
        # A same-day daily stop must not immediately re-flatten the resumed sleeve.
        self.store.set("locked_until", "")
        return ResumeResult(True, ps.nav, month, stamp)

    # -- counters --------------------------------------------------------------

    def trades_today(self, now: datetime) -> int:
        if self.store.get("trades_today_date") != _gulf_date(now):
            return 0
        return int(_fnum(self.store.get("trades_today")))

    def orders_today(self, now: datetime) -> int:
        return int(_fnum(self.store.get(f"orders_day_{_gulf_date(now)}")))

    def turnover_today(self, now: datetime) -> float:
        return _fnum(self.store.get(f"turnover_day_{_gulf_date(now)}"))

    def fees_this_month(self, now: datetime) -> float:
        return _fnum(self.store.get(f"fees_month_{_gulf_month(now)}"))

    def record_entry_fill(self, now: datetime) -> None:
        today = _gulf_date(now)
        if self.store.get("trades_today_date") != today:
            self.store.set("trades_today_date", today)
            self.store.set("trades_today", "1")
        else:
            self.store.set("trades_today", str(int(_fnum(self.store.get("trades_today"))) + 1))

    def record_order_fill(self, now: datetime, *, notional: float, fee_usdt: float = 0.0,
                          risk_exit: bool = False) -> None:
        """Update the run-scoped churn counters the gate reads back.

        Risk exits still add to turnover and the fee budget (they are real costs) but
        never to the discretionary order count, so a flatten can never be starved by
        the same limit it just tripped.
        """
        today, month = _gulf_date(now), _gulf_month(now)
        if not risk_exit:
            self.store.set(f"orders_day_{today}",
                           str(int(_fnum(self.store.get(f"orders_day_{today}"))) + 1))
        self.store.set(f"turnover_day_{today}",
                       repr(_fnum(self.store.get(f"turnover_day_{today}")) + abs(notional)))
        if fee_usdt:
            self.store.set(f"fees_month_{month}",
                           repr(_fnum(self.store.get(f"fees_month_{month}")) + abs(fee_usdt)))

    # -- read-only views (console) ---------------------------------------------

    def utilisation(self, ps: PortfolioState) -> dict[str, dict[str, float]]:
        """Per-limit ``{used, limit, headroom}`` for the Risk page meters."""
        cfg = self.cfg
        nav = max(ps.nav, _EPS)
        rows = {
            "trades_per_day": (float(self.trades_today(ps.now)), float(cfg.max_trades_per_day)),
            "orders_per_day": (float(self.orders_today(ps.now)), float(cfg.max_orders_per_day)),
            "turnover_day": (self.turnover_today(ps.now) / nav, cfg.max_turnover_pct_per_day),
            "fee_budget": (self.fees_this_month(ps.now) / nav, cfg.max_fee_pct_per_month),
            "gross_cap": (ps.gross / nav, cfg.gross_cap),
        }
        out = {
            name: {"used": used, "limit": limit, "headroom": limit - used,
                   "pct": (used / limit) if limit else 0.0}
            for name, (used, limit) in rows.items()
        }
        # The USDT floor is a MINIMUM, so headroom is how far above it we sit.
        free_w = ps.free_usdt / nav
        out["usdt_floor"] = {"used": free_w, "limit": cfg.usdt_floor,
                             "headroom": free_w - cfg.usdt_floor,
                             "pct": (cfg.usdt_floor / free_w) if free_w else 1.0}
        return out
