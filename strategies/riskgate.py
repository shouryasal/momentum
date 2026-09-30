"""The deterministic risk gate. TIER 2 — human-only. STDLIB ONLY (runs inside the
freqtrade container; ops/ is not mounted there).

Pure logic: RiskGate takes a GateConfig plus injected providers (flags, staleness,
kill) and a StateStore, so every limit is unit-testable without freqtrade. The
freqtrade adapter (earn_base.py) wires the callbacks to this.

Fail-closed rules (canonical contracts #5/#6): an unreadable or stale flags file
blocks entries; missing freshness data blocks entries; a NAV the adapter could not
compute blocks entries.

The monthly stop has ONE expiry and it is the thing it measures. ``monthly_loss_stop``
is a drawdown *from the Gulf-month anchor*; when the Gulf calendar month turns, that
anchor is re-taken at the current NAV and the condition that set the lock no longer
exists — so the lock is released with it. A drawdown in May does not block December.
Per mode (``GateConfig.monthly_lock_release``):

* **LIVE** — ``human_or_month_end``. The sleeve is paused and a human may resume it
  early through ``ops.lib.risk_resume`` (which also re-anchors); if nobody does, the
  pause still ends at the Gulf month boundary. It is a visible, resumable state, never
  a lock that outlives its own reason.
* **TEST / backtest** — ``month_end``. There is no human, so the boundary is the only
  release. Without this a backtest measures the lock and not the strategy: one -10%
  month in 2021 silenced SleeveA for the remaining 5.3 years of the sample.

The daily stop expires on its own ``locked_until`` timestamp (``daily_stop_lock_hours``
from the moment it fired), the re-entry cooldown on ``reentry_cooldown_hours``, and the
freqtrade protections on candle counts — every lock in this file is bounded by the
condition that set it.

The daily stop's RESPONSE is ``risk.daily_loss_response`` (docs/design/crisis-policy.md
§0; docs/design/risk-and-ladder-2026-09-29.md). ``halve`` — the shipped value — asks the
adapter for one 50% trim of every open position (``LoopActions.reduce``,
``RiskGate.reduce_pending``) and locks entries; ``flatten`` is the legacy shape
(``LoopActions.flatten``, ``flatten_pending``). Measured at the identical -3% trigger over
2019-2026 with costs on: flatten -5.23% CAGR, halve +13.05%. The monthly stop still
flattens; only the daily response was measured and only it moves.

``min_edge`` (``risk.min_edge``; analogue-timing.md §4.4-4.5) is a plan-shape check: the
smallest profit a sleeve's take-profit mechanics will book at must clear the round trip by
the configured multiple, or every entry of that plan is refused. It is never an exit check.
"""

from __future__ import annotations

import json
import math
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
    "reconcile", "exit_only", "tier", "trades_per_day", "orders_per_day", "turnover_day",
    "fee_budget", "min_edge", "min_notional", "step_size", "order_notional",
    "entries_per_trade", "min_position", "max_positions", "satellite_count",
    "satellite_gross", "weight_cap", "beta_cap", "corr_cap", "gross_cap", "usdt_floor",
)

#: What the daily stop does to the book once it has fired (``risk.daily_loss_response``).
#: ``hold`` sells nothing and locks entries; ``halve`` trims every open position by
#: :data:`DAILY_HALVE_FRACTION` and locks; ``flatten`` sells everything and locks.
#: crisis-policy.md §0 measured all three at the same -3% trigger over 2019-2026, costs
#: on: hold-and-stop-buying +30.53% CAGR, halve +13.05%, flatten -5.23%. The shipped value
#: is ``hold`` — and it is the value that says what the book does, because the adapter
#: that would trim positions on ``reduce`` was never built; a config that said ``halve``
#: over a book that held was a lie the integration review caught on 2026-09-29.
DAILY_RESPONSES = ("hold", "halve", "flatten")
DAILY_HALVE_FRACTION = 0.5

#: Tier names the universe snapshot may assign. ``core`` is BTC/ETH and is never rotated;
#: ``major`` and ``satellite`` are the tradeable tiers (wide-universe.md §2.2). Anything
#: else — including a watchlist-only name and an asset the snapshot has never heard of —
#: resolves to a cap of zero and the ``tier`` check refuses it.
TRADEABLE_TIERS = ("core", "major", "satellite")
SATELLITE_TIER = "satellite"

# The subset that also gates a *discretionary* exit (a trim or a TP rung). Risk exits
# are never blocked — see mechanics.is_risk_exit.
EXIT_CHECK_ORDER = ("orders_per_day", "turnover_day", "fee_budget")

# The flag name reconciliation raises; it maps to its own check, not to 'blackout'.
RECONCILE_FLAG = "reconcile_mismatch"


def default_run_id(sleeve: str) -> str:
    """The run id used when no transition has minted one — a fresh checkout.

    Stdlib mirror of ``ops.gen_freqtrade_config.default_run_id``; ``strategies/`` is
    mounted flat into the container and may not import ``ops``, and
    ``tests/test_strategies/test_gate_config.py`` cross-checks that the two agree.

    They *must* agree, because this value is the ``risk_state`` namespace. With ``""``
    here and ``test-<s>-000`` there, the same machine wrote the gate's anchors and locks
    under un-namespaced keys while every host-side reader that consults the generator's
    name looked under ``run:test-<s>-000:`` — two shapes of the same row, which is why
    ``ops.lib.mode_view.risk_state`` has to read both. It still reads both, for rows
    already on disk; nothing creates the second shape any more.
    """
    return f"test-{str(sleeve).lower()}-000"


# --------------------------------------------------------------------------- universe

@dataclass(frozen=True)
class UniverseView:
    """The point-in-time universe the gate enforces against, keyed by BASE ASSET.

    Rendered into ``config/riskgate.json`` by ``ops.gen_freqtrade_config`` from the
    resolver's snapshot (``knowledge/universe/<date>.json``, package U1), so the live bot
    and the backtest read the same artefact by construction — freqtrade's dynamic
    pairlists are all ``SupportsBacktesting.NO`` and cannot give us that
    (wide-universe.md §5.3).

    Absent a snapshot this degrades to exactly the two-asset world: every asset in
    ``risk.max_weight`` is ``core``, nothing is a satellite, and no new check can fire.
    That is the fallback, not the target.

    ``caps`` is what the RESOLVER proposed. It is never the answer on its own —
    :meth:`cap_for` takes the tighter of it and the human ceiling, so a corrupted or
    over-generous snapshot can only ever shrink a cap.
    """

    tiers: dict[str, str] = field(default_factory=dict)         # asset -> tier
    caps: dict[str, float] = field(default_factory=dict)        # asset -> resolver's cap
    scores: dict[str, float] = field(default_factory=dict)      # asset -> satellite score
    exit_only: frozenset[str] = frozenset()
    filters: dict[str, dict[str, float]] = field(default_factory=dict)  # exchange filters
    date: str = ""
    sha256: str = ""

    def tier_of(self, asset: str) -> str:
        if asset in self.exit_only:
            return "exit_only"
        return self.tiers.get(asset, "")

    def is_tradeable(self, asset: str) -> bool:
        return asset not in self.exit_only and self.tiers.get(asset, "") in TRADEABLE_TIERS

    def is_satellite(self, asset: str) -> bool:
        return self.tiers.get(asset, "") == SATELLITE_TIER

    def filter_floor(self, asset: str) -> float:
        """Smallest order notional this asset's own exchange filters permit, in USDT.

        ``minNotional`` is $5 for 450 pairs and $1 for 30, so Binance is rarely the binding
        constraint — but the tail of a 100-name watchlist is not surveyed, and one step of
        ``LOT_SIZE`` can be worth real money (``ZEC``: $1.56 of notional per step). The
        gate re-checks both because the gate, not freqtrade's ``PrecisionFilter``, is the
        authority (wide-universe.md §2.4).
        """
        f = self.filters.get(asset) or {}
        try:
            return max(float(f.get("min_notional", 0.0) or 0.0),
                       float(f.get("step_notional", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0


def _resolve_universe(uni: dict[str, Any], max_weight: dict[str, Any]) -> UniverseView:
    """Build the :class:`UniverseView` from ``riskgate.json``'s ``universe`` block."""
    snap = uni.get("snapshot")
    if not isinstance(snap, dict):
        # No snapshot: the assets that carry an explicit human cap are the whole universe,
        # all core. This is today's BTC/ETH world and nothing about it changes.
        return UniverseView(tiers={str(a): "core" for a in (max_weight or {})})
    def _fmap(key: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for k, v in (snap.get(key) or {}).items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    filters: dict[str, dict[str, float]] = {}
    for asset, spec in (snap.get("filters") or {}).items():
        if isinstance(spec, dict):
            filters[str(asset)] = {str(k): v for k, v in spec.items()}
    return UniverseView(
        tiers={str(k): str(v) for k, v in (snap.get("tiers") or {}).items()},
        caps=_fmap("caps"),
        scores=_fmap("scores"),
        exit_only=frozenset(str(a) for a in (snap.get("exit_only") or [])),
        filters=filters,
        date=str(snap.get("date") or ""),
        sha256=str(snap.get("sha256") or ""),
    )


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class GateConfig:
    sleeve: str                      # 'a' | 'b'
    pairs: tuple[str, ...]
    weight_caps: dict[str, float]    # PAIR-keyed, resolved from tier ceilings + max_weight
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
    max_orders_per_day: int = 16
    max_turnover_pct_per_day: float = 0.50
    #: A reduction of at least this share of NAV is exempt from the churn/turnover/fee budget.
    #: Exempting by SIZE rather than by reason-name is the point — see
    #: :meth:`RiskGate.check_discretionary_exit`.
    derisk_exempt_pct: float = 0.10
    # --- wide universe (wide-universe.md §2.2-2.4) -------------------------------
    universe: UniverseView = field(default_factory=UniverseView)
    tier_caps: dict[str, float] = field(default_factory=dict)   # tier -> human ceiling
    max_weight: dict[str, float] = field(default_factory=dict)  # asset -> human ceiling
    max_open_positions: int = 8
    # 0 / 0.0, not the historical 4 / 0.10: a riskgate.json missing these keys must DISABLE
    # satellites, not silently restore the pre-2026-09-29 wide-universe limits. A fallback
    # that widens a risk limit is the one direction a default may never fail in.
    max_satellite_positions: int = 0
    max_satellite_gross: float = 0.0
    max_beta_to_btc: float = 1.30
    max_avg_pairwise_corr: float = 0.70
    min_position_pct_nav: float = 0.02
    max_fee_pct_per_month: float = 0.01
    market_entries_allowed: bool = False
    protection_drawdown_lookback: int = 6
    protection_drawdown_trade_limit: int = 2
    reconcile_tolerance_pct: float = 0.005
    reconcile_dust_usdt: float = 10.0
    reconcile_block_on_mismatch: bool = True
    # --- the daily stop's response (risk.daily_loss_response) -----------------------
    #: ``flatten`` when the rendered config predates the key, so an old riskgate.json
    #: keeps the behaviour it was rendered with; the generator always writes the key.
    daily_loss_response: str = "flatten"
    # --- the minimum-edge gate (risk.min_edge; analogue-timing.md §4.4-4.5) ---------
    #: ``multiple`` 0.0 = the check is off, which is what a config without the block gets.
    min_edge_round_trip_cost_pct: float = 0.0
    min_edge_multiple: float = 0.0
    # --- crisis-policy.md Tier 1 (risk.crisis) ---------------------------------------
    crisis_block_entries_hours: int = 48
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

    @property
    def monthly_lock_release(self) -> str:
        """How a monthly stop ends, stated per mode (see the module docstring).

        ``human_or_month_end`` in LIVE — a human may resume early via
        ``ops.lib.risk_resume``, and the Gulf month boundary releases it regardless.
        ``month_end`` in TEST and in a backtest, where there is no human to ask.
        Both expire; neither outlives the month the drawdown was measured over.
        """
        return "human_or_month_end" if self.is_live else "month_end"

    @classmethod
    def load(cls, path: str | Path | None = None, sleeve: str | None = None,
             runtime_path: str | Path | None = None) -> GateConfig:
        """Committed ``riskgate.json`` merged with this machine's ``runtime-<s>.json``.

        The runtime file (``$EARN_RUNTIME``) is the only thing that can say "live"; it
        is rendered from a signed mode state. Its absence or corruption leaves the
        committed TEST baseline in place — fail closed by construction, and that baseline
        now includes :func:`default_run_id`, the same name the generator would have
        rendered, so the gate never writes un-namespaced ``risk_state`` rows.
        """
        p = Path(path or os.environ.get("EARN_RISKGATE", "config/riskgate.json"))
        raw = json.loads(p.read_text())
        risk, uni, ex = raw["risk"], raw["universe"], raw["execution"]
        cp = raw["container_paths"]
        sleeve_id = (sleeve or os.environ.get("EARN_SLEEVE", "a")).lower()
        quote = str(uni.get("quote") or "USDT")
        max_weight = {str(k): float(v) for k, v in (risk.get("max_weight") or {}).items()
                      if k != "default"}
        tier_caps = {str(k): float(v) for k, v in (risk.get("tier_caps") or {}).items()}
        universe = _resolve_universe(uni, max_weight)
        # Every pair the gate knows about: the whitelist plus anything the snapshot has a
        # tier for, so a pair we still HOLD but no longer whitelist still resolves a cap
        # (it resolves to zero, and exit_only then refuses the entry — which is the point).
        pairs = tuple(uni["pairs"])
        known = {p.split("/")[0] for p in pairs} | set(universe.tiers) | set(universe.exit_only)
        caps = {
            f"{asset}/{quote}": _resolve_cap(asset, universe, tier_caps, max_weight)
            for asset in sorted(known)
        }
        caps.update({p: _resolve_cap(p.split("/")[0], universe, tier_caps, max_weight)
                     for p in pairs})
        trading_all = raw.get("trading") or {}
        trading = dict((trading_all.get("sleeves") or {}).get(sleeve_id) or {})
        runtime = _load_runtime(runtime_path, sleeve_id)
        protections = (risk.get("protections") or {}).get("max_drawdown") or {}
        reconcile = risk.get("reconcile") or {}
        min_edge = risk.get("min_edge") or {}
        crisis = risk.get("crisis") or {}
        response = str(risk.get("daily_loss_response") or "flatten").lower()
        if response not in DAILY_RESPONSES:
            raise ValueError(
                f"risk.daily_loss_response {response!r} is not one of {DAILY_RESPONSES}"
            )
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
            max_orders_per_day=int(risk.get("max_orders_per_day", 16)),
            universe=universe,
            tier_caps=tier_caps,
            max_weight=max_weight,
            max_open_positions=int(risk.get("max_open_positions", 8)),
            max_satellite_positions=int(risk.get("max_satellite_positions", 0)),
            max_satellite_gross=float(risk.get("max_satellite_gross", 0.0)),
            max_beta_to_btc=float(risk.get("max_beta_to_btc", 1.30)),
            max_avg_pairwise_corr=float(risk.get("max_avg_pairwise_corr", 0.70)),
            min_position_pct_nav=float(risk.get("min_position_pct_nav", 0.02)),
            max_turnover_pct_per_day=float(risk.get("max_turnover_pct_per_day", 0.50)),
            derisk_exempt_pct=float(risk.get("derisk_exempt_pct", 0.10)),
            max_fee_pct_per_month=float(risk.get("max_fee_pct_per_month", 0.01)),
            market_entries_allowed=bool(risk.get("market_entries_allowed", False)),
            protection_drawdown_lookback=int(protections.get("lookback_candles", 6)),
            protection_drawdown_trade_limit=int(protections.get("trade_limit", 2)),
            reconcile_tolerance_pct=float(reconcile.get("tolerance_pct", 0.005)),
            reconcile_dust_usdt=float(reconcile.get("dust_usdt", 10.0)),
            reconcile_block_on_mismatch=bool(reconcile.get("block_on_mismatch", True)),
            daily_loss_response=response,
            min_edge_round_trip_cost_pct=float(min_edge.get("round_trip_cost_pct", 0.0) or 0.0),
            min_edge_multiple=float(min_edge.get("multiple", 0.0) or 0.0),
            crisis_block_entries_hours=int(crisis.get("block_entries_hours", 48) or 48),
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
            run_id=str(runtime.get("run_id") or default_run_id(sleeve_id)),
            seed_usdt=float(runtime.get("seed_usdt") or 0.0),
            require_approval=bool(runtime.get("require_approval", False)),
        )

    # -- wide-universe accessors ------------------------------------------------

    def cap_for(self, pair: str) -> float:
        """Weight cap for a pair. A pair the gate has never resolved caps at zero."""
        cap = self.weight_caps.get(pair)
        if cap is not None:
            return cap
        return _resolve_cap(asset_of(pair), self.universe, self.tier_caps, self.max_weight)

    def tier_of(self, pair: str) -> str:
        return self.universe.tier_of(asset_of(pair))

    def is_satellite(self, pair: str) -> bool:
        return self.universe.is_satellite(asset_of(pair))

    def notional_floor(self, pair: str) -> float:
        """The binding minimum order notional: Earn's own floor or the exchange's."""
        return max(self.min_notional, self.universe.filter_floor(asset_of(pair)))

    def mechanics(self, path: str, default: Any = None) -> Any:
        """One resolved mechanics value, e.g. ``cfg.mechanics('dca.step_pct')``."""
        node: Any = self.trading
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- the minimum-edge gate -----------------------------------------------------

    @property
    def min_edge_floor(self) -> float:
        """``multiple x round_trip_cost_pct``; 0.0 means the check is off."""
        return max(self.min_edge_multiple, 0.0) * max(self.min_edge_round_trip_cost_pct, 0.0)

    def min_booked_target(self) -> float | None:
        """The smallest profit this sleeve's plan will book at, or ``None`` if never.

        The lowest take-profit rung and the lowest ROI level, whichever is smaller: that
        is the exit the plan takes most often and the one the cost floor has to be measured
        against (analogue-timing.md §4.4: at a 0.33-day hold the fees *were* the loss).
        Stdlib mirror of ``ops.config.min_booked_target``; the two must agree.
        """
        tp = self.mechanics("take_profit") or {}
        targets: list[float] = []
        for rung in (tp.get("ladder") or []) if isinstance(tp, dict) else []:
            try:
                targets.append(float(rung.get("at_profit_pct")))
            except (AttributeError, TypeError, ValueError):
                continue
        roi = tp.get("roi_table") if isinstance(tp, dict) else None
        for value in (roi or {}).values():
            try:
                targets.append(float(value))
            except (TypeError, ValueError):
                continue
        return min(targets) if targets else None

    def min_edge_ok(self) -> bool:
        """Does the plan's smallest booked target clear the configured cost multiple?"""
        floor = self.min_edge_floor
        if floor <= 0:
            return True
        target = self.min_booked_target()
        return target is None or target + _EPS >= floor


def asset_of(pair: str) -> str:
    return str(pair).split("/")[0]


# ------------------------------------------------------- correlation / beta (stdlib)
#
# The gate computes these itself, from returns the adapter already has, at entry time.
# They are not model inputs and no proposal can waive them. Pure float maths on purpose:
# strategies/ is mounted flat into the freqtrade container and numpy is not guaranteed
# there, and a risk check that can ImportError is not a risk check.

def _pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two equal-length series; ``None`` when undefined."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= _EPS or vb <= _EPS:
        return None            # a constant series has no correlation, not a correlation of 0
    return cov / ((va ** 0.5) * (vb ** 0.5))


def avg_pairwise_corr(series: dict[str, list[float]]) -> float | None:
    """Average pairwise correlation of a held book. ``None`` when it cannot be measured.

    Fewer than two measurable names is not a correlated book — it is one position, which
    the per-asset cap already bounds — so the answer is ``None`` and the check passes.
    """
    names = sorted(series)
    vals: list[float] = []
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            c = _pearson(series[x], series[y])
            if c is not None:
                vals.append(c)
    return sum(vals) / len(vals) if vals else None


def beta_to(series: list[float], benchmark: list[float]) -> float | None:
    """Realised beta of ``series`` against ``benchmark`` over their common window."""
    n = min(len(series), len(benchmark))
    if n < 2:
        return None
    s, b = series[-n:], benchmark[-n:]
    mb = sum(b) / n
    vb = sum((y - mb) ** 2 for y in b)
    if vb <= _EPS:
        return None
    ms = sum(s) / n
    cov = sum((x - ms) * (y - mb) for x, y in zip(s, b, strict=True))
    return cov / vb


def portfolio_beta(weights: dict[str, float], series: dict[str, list[float]],
                   benchmark: list[float]) -> float | None:
    """Exposure-weighted beta of the risk book. ``None`` when nothing is measurable.

    Weights are position values; the result is normalised by the measurable exposure, so
    a name with no history neither inflates nor deflates the answer. A pure-BTC book
    measures 1.0 and can therefore never breach a cap above 1.0 — which is the intent:
    the cap exists to stop an eight-name alt book being a 1.5x levered BTC position
    wearing eight names (wide-universe.md §2.3).
    """
    num = 0.0
    den = 0.0
    for name, w in weights.items():
        if w <= 0:
            continue
        b = beta_to(series.get(name) or [], benchmark)
        if b is None:
            continue
        num += w * b
        den += w
    return num / den if den > _EPS else None


def _resolve_cap(asset: str, universe: UniverseView, tier_caps: dict[str, float],
                 max_weight: dict[str, float]) -> float:
    """One asset's weight cap. **Unknown means zero, never a default.**

    Resolution, tightest wins (wide-universe.md §2.2):

    1. ``exit_only`` ⇒ 0.0. A position being wound down can never be increased.
    2. the human's explicit ``risk.max_weight[asset]``, if there is one;
    3. the human's ``risk.tier_caps[tier]`` ceiling for the tier the snapshot assigned;
    4. the resolver's own proposed cap, which can only ever shrink the answer;
    5. no tier and no explicit entry ⇒ **0.0**, and the ``tier`` check refuses the order.

    The deleted ``max_weight.default`` is the whole reason this function exists: it gave
    every asset that appeared in the universe a 30% cap without anyone deciding so.
    """
    if asset in universe.exit_only:
        return 0.0
    tier = universe.tiers.get(asset, "")
    ceilings = [c for c in (max_weight.get(asset),
                            tier_caps.get(tier) if tier in TRADEABLE_TIERS else None,
                            universe.caps.get(asset)) if c is not None]
    if not ceilings:
        return 0.0
    return max(float(min(ceilings)), 0.0)


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

    A source value may also be an object carrying ``latest_utc``, and optionally the
    ``grace_minutes`` its writer stamped: how long after its newest datum that feed is still
    current, which only the writer knows. Age is ``max(age - grace, 0)`` over every source,
    where grace is the stamped value, else one whole timeframe for a ``candles_<tf>`` member,
    else zero. Missing, unparseable or empty ⇒ ``+inf`` (fail-closed: no entries until data
    flows). This mirrors ``ops.lib.freshness.grace_of`` and the two must not drift.
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
        ages.append(age - _grace_minutes(name, value))
    if not ages:
        return float("inf")
    return max(max(ages), 0.0)


#: Ceiling on a writer-stamped grace. Mirrors ``ops.lib.freshness.MAX_GRACE_MINUTES``.
MAX_GRACE_MINUTES = 1440.0


def _grace_minutes(name: str, value: object) -> float:
    """The in-container half of ``ops.lib.freshness.grace_of``. Keep the two identical.

    A writer-stamped ``grace_minutes`` wins; otherwise a ``candles_<tf>`` member is allowed
    one whole timeframe; otherwise zero. A non-positive, non-finite or unparseable stamp is
    zero and anything larger than ``MAX_GRACE_MINUTES`` is clamped to it, so a corrupt file
    cannot *widen* the allowance — an ``Infinity`` here would switch the staleness check off
    altogether, and that direction has to fail closed.
    """
    if isinstance(value, dict):
        raw = value.get("grace_minutes")
        if raw is not None:
            try:
                g = float(raw)
            except (TypeError, ValueError):
                g = 0.0
            if math.isfinite(g) and g > 0:
                return min(g, MAX_GRACE_MINUTES)
    if name.startswith("candles_"):
        return _tf_minutes(name.split("_", 1)[1])
    return 0.0


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
    #: pair -> USDT resting in that pair's UNFILLED entry orders. It is already inside
    #: ``reserved_usdt`` (and therefore inside ``nav``); this breaks it down per pair so a
    #: sizing rule can see money that is committed but not yet a position.
    pending: dict[str, float] = field(default_factory=dict)      # pair -> resting USDT

    def committed(self, pair: str) -> float:
        """Position value PLUS anything resting in an unfilled entry order for ``pair``.

        The number every "how much more of this do I want?" rule must use. Freqtrade 2026.8
        creates a trade with ``amount=0`` and calls ``adjust_trade_position`` while the entry
        order is still open, so ``positions[pair]`` is 0 for a fully-placed target: a gap of
        ``target × NAV − position`` then reads as "nothing bought yet" and the rebalance,
        DCA and top-up paths place the order a second time. On 2026-09-23 Sleeve B bought
        BTC and ETH twice inside 68 ms, ended at 75% of NAV against a 25% target, and paid
        the round trip twice (−27.56 USDT, 15.01 of it fees). The gate could not catch it:
        ``weight_cap`` and ``gross_cap`` were reading the same position view.
        """
        return float(self.positions.get(pair, 0.0)) + float(self.pending.get(pair, 0.0))

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
    #: The daily stop fired under ``daily_loss_response: halve``: trim every open position
    #: by ``reduce_fraction`` (a risk exit, ``reduce_reason``) and lock entries until
    #: ``lock_until``. Never set together with ``flatten``. The adapter acts on it through
    #: :meth:`RiskGate.reduce_pending`, one partial exit per trade per stop.
    reduce: bool = False
    reduce_fraction: float = 0.0
    reduce_reason: str = ""
    monthly_lock: bool = False
    #: The Gulf month boundary released a monthly stop on this tick. The adapter
    #: journals it so the release is as visible in the record as the stop was.
    monthly_unlock: bool = False
    unlocked_month: str = ""


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
                 flags_provider=None, staleness_provider=None, kill_provider=None,
                 returns_provider=None):
        self.cfg = cfg
        self.store = NamespacedStateStore(store, cfg.run_id) if cfg.run_id else store
        self._flags = flags_provider or (
            lambda pair, now: flags_blocked(cfg.flags_path, pair, now))
        self._age = staleness_provider or (
            lambda now: data_age_minutes(cfg.freshness_path, now))
        self._kill = kill_provider or (lambda: Path(cfg.kill_path).exists())
        #: ``pair -> recent daily returns`` (oldest first), or ``None`` when the adapter
        #: has no history for it. Injected so the gate stays pure and freqtrade-free; the
        #: default answers "no history", under which the beta and correlation checks pass.
        self._returns = returns_provider or (lambda pair: None)

    def kill_engaged(self) -> bool:
        """Is the kill switch engaged? The public name, so callers stop reaching for ``_kill``.

        What KILL means, in one sentence, because the codebase used to answer it differently in
        four places: **no new risk, and no order whose size came from somewhere else.** A sleeve
        may still reduce a position when both the trigger and the size come from what the bot
        observes itself — price, NAV, its own candles — or from a direct human instruction. It
        may not originate a sell sized by a file another process wrote, because KILL is engaged
        exactly when that other process's output is in doubt: a human has said stop, or
        ``ops/healthcheck.py`` found a mode mismatch, which is an integrity failure.

        So stops, trailing stops, the daily-loss flatten, a human force-exit and the
        price-sized take-profit ladder all keep working under KILL. The ensemble trim and
        SleeveB's proposal-driven sells do not. See ``docs/design/decisions-2026-09-30.md`` §1.
        """
        return bool(self._kill())

    # -- book shape ------------------------------------------------------------

    def _held(self, ps: PortfolioState) -> dict[str, float]:
        """Pairs actually held, in USDT. Dust is not a position for counting purposes."""
        floor = max(self.cfg.dust_weight * max(ps.nav, _EPS), 0.0)
        return {p: v for p, v in ps.positions.items() if v > floor}

    def _book_after(self, pair: str, stake: float, ps: PortfolioState) -> dict[str, float]:
        book = self._held(ps)
        book[pair] = book.get(pair, 0.0) + max(stake, 0.0)
        return book

    def _series_for(self, book: dict[str, float]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for p in book:
            try:
                r = self._returns(p)
            except Exception:  # noqa: BLE001 — a missing series must not break the gate
                r = None
            if r:
                series = [float(x) for x in r]
                if len(series) >= 2:
                    out[p] = series
        return out

    def _benchmark_series(self) -> list[float]:
        """BTC's returns — the benchmark the beta cap is measured against."""
        for pair in self.cfg.pairs:
            if asset_of(pair) == "BTC":
                try:
                    return [float(x) for x in (self._returns(pair) or [])]
                except Exception:  # noqa: BLE001
                    return []
        try:
            return [float(x) for x in (self._returns("BTC/USDT") or [])]
        except Exception:  # noqa: BLE001
            return []

    # -- entry -----------------------------------------------------------------

    def check_entry(self, pair: str, stake: float, ps: PortfolioState) -> GateDecision:
        checks: dict[str, bool] = {}
        reason = "ok"
        cfg = self.cfg

        checks["nav_valid"] = bool(ps.valid) and ps.nav > 0
        checks["kill"] = not self.kill_engaged()
        checks["monthly_lock"] = not self._monthly_lock_active(ps.now)
        checks["daily_lock"] = not self._daily_locked(ps.now)
        blocked, flag = self._flags(pair, ps.now)
        reconcile_hit = blocked and flag == RECONCILE_FLAG
        checks["blackout"] = not (blocked and not reconcile_hit)
        checks["staleness"] = self._age(ps.now) <= cfg.staleness_minutes
        checks["reconcile"] = not (reconcile_hit and cfg.reconcile_block_on_mismatch)
        asset = asset_of(pair)
        checks["exit_only"] = asset not in cfg.universe.exit_only
        # Unknown asset means cap ZERO, not a default. A name reaches this line tradeable
        # only because the resolver put a tier on it in the point-in-time snapshot.
        checks["tier"] = cfg.universe.is_tradeable(asset) and cfg.cap_for(pair) > 0.0
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
        # A plan-shape refusal (analogue-timing.md §4.5, §7.1): the smallest profit this
        # sleeve will book at must clear the round trip by the configured multiple, or
        # the trade is paying the venue to exist. Static per sleeve, so it refuses every
        # entry of a plan that cannot pay for itself — which is the point. Never an exit
        # check: a cost rule must not be able to veto a stop.
        checks["min_edge"] = cfg.min_edge_ok()
        checks["min_notional"] = stake >= cfg.min_notional
        # The asset's OWN exchange filters, re-checked here because the gate is the
        # authority: below one LOT_SIZE step or the symbol's minNotional there is no order.
        checks["step_size"] = stake + _EPS >= cfg.universe.filter_floor(asset)
        checks["order_notional"] = stake / nav <= cfg.max_order_notional_pct + _EPS
        checks["entries_per_trade"] = (
            ps.entries_used.get(pair, 0) < cfg.max_entries_per_trade
        )
        pos = ps.positions.get(pair, 0.0)
        # A position smaller than min_position_pct_nav can be opened and closed but never
        # MANAGED — at the $1,000 live seed a 2% position is $20 and a half-trim of it is
        # $10, under the $25 min_notional floor — so it is not opened (wide-universe.md
        # §2.4). Only OPENING is tested: an add is by definition making an existing
        # position bigger, and refusing adds under 2% of NAV would ban the scheduled DCA
        # from ever finishing a position it is deliberately building in chunks.
        opening = pos <= cfg.dust_weight * nav
        checks["min_position"] = (
            not opening or (pos + stake) / nav >= cfg.min_position_pct_nav - _EPS
        )
        book = self._book_after(pair, stake, ps)
        checks["max_positions"] = len(book) <= cfg.max_open_positions
        sats = {p: v for p, v in book.items() if cfg.is_satellite(p)}
        checks["satellite_count"] = len(sats) <= cfg.max_satellite_positions
        checks["satellite_gross"] = (
            sum(sats.values()) / nav <= cfg.max_satellite_gross + _EPS
        )
        checks["weight_cap"] = (pos + stake) / nav <= cfg.cap_for(pair) + _EPS
        # Beta and correlation are measured on the book this entry would CREATE, so the
        # gate refuses the incremental order that crosses the line rather than the book
        # that has already crossed it. Unmeasurable (no history yet, one name, a constant
        # series) is not a breach: it passes, and the tier cap plus max_satellite_gross
        # are what bound the damage in that window.
        series = self._series_for(book)
        beta = portfolio_beta(book, series, self._benchmark_series())
        checks["beta_cap"] = beta is None or beta <= cfg.max_beta_to_btc + _EPS
        corr = avg_pairwise_corr(series)
        checks["corr_cap"] = corr is None or corr <= cfg.max_avg_pairwise_corr + _EPS
        gross = ps.gross
        checks["gross_cap"] = (gross + stake) / nav <= cfg.gross_cap + _EPS
        checks["usdt_floor"] = ps.free_usdt - stake >= cfg.usdt_floor * nav - _EPS

        for name in CHECK_ORDER:
            if not checks[name]:
                qualifier = {
                    "blackout": flag, "weight_cap": pair, "gross_cap": pair,
                    "entries_per_trade": pair, "nav_valid": ps.reason or None,
                    "exit_only": asset, "tier": asset, "step_size": pair,
                    "min_edge": self._min_edge_qualifier(),
                }.get(name)
                reason = f"{name}:{qualifier}" if qualifier else name
                break
        return GateDecision(allowed=(reason == "ok"), reason=reason, checks=checks)

    def _min_edge_qualifier(self) -> str | None:
        """``target<floor`` in percent, so the journalled reason carries the arithmetic."""
        target = self.cfg.min_booked_target()
        if target is None:
            return None
        return f"{target * 100:.2f}%<{self.cfg.min_edge_floor * 100:.2f}%"

    def cap_stake(self, pair: str, proposed: float, ps: PortfolioState) -> float:
        """Shrink a proposed stake to the tightest headroom; below the floor -> 0.

        The satellite-gross headroom is here as well as in :meth:`check_entry` so a
        satellite order is *trimmed* to what the 10% sleeve can still hold rather than
        refused outright — the cap should shape the book, not stop it dead. Every other
        wide-universe control (tier, exit_only, position count, beta, correlation) is a
        yes/no about whether this order may exist at all, so it has no headroom to
        express and stays a refusal in ``check_entry``.
        """
        if not ps.valid:
            return 0.0
        cfg = self.cfg
        nav = max(ps.nav, _EPS)
        pos = ps.positions.get(pair, 0.0)
        headrooms = [
            cfg.cap_for(pair) * nav - pos,
            cfg.gross_cap * nav - ps.gross,
            ps.free_usdt - cfg.usdt_floor * nav,
            cfg.max_order_notional_pct * nav,
            max(cfg.max_turnover_pct_per_day * nav - self.turnover_today(ps.now), 0.0),
            proposed,
        ]
        if cfg.is_satellite(pair):
            sats = sum(v for p, v in self._held(ps).items() if cfg.is_satellite(p))
            headrooms.append(cfg.max_satellite_gross * nav - sats)
        stake = max(min(headrooms), 0.0)
        return stake if stake >= cfg.notional_floor(pair) else 0.0

    # -- exit ------------------------------------------------------------------

    def check_discretionary_exit(self, pair: str, stake: float, ps: PortfolioState,
                                 exit_reason: str) -> GateDecision:
        """Churn/turnover/fee gate for a *discretionary* exit (TP rung, rebalance trim).

        Two exemptions, and the second one is the important one.

        A **risk exit** — stop, trailing stop, daily/monthly flatten, KILL, ``target_zero`` —
        is never blocked. That is matched by name, through :func:`mechanics.is_risk_exit`.

        A **large reduction** is never blocked either, whatever it is called, once it reaches
        ``risk.derisk_exempt_pct`` of NAV. Matching only by name was a money bug with three
        reinforcing halves. ``exit_signal`` — the value freqtrade actually passes for
        ``populate_exit_trend``, verified against ``freqtrade.enums.ExitType`` — is in neither
        ``RISK_EXIT_REASONS`` nor the ``risk_stop``/``stop_loss``/``trailing_stop``/``emergency``
        prefixes, so **SleeveA's MA200 flip, its one signal-driven sell and a 100% close, was
        refusable by a fee counter**. ``abs(stake)`` sits in the turnover numerator below, so the
        bigger the de-risk the likelier the refusal. ``ACTION_PRIORITY`` ranks ``add`` above
        ``rebalance``, so risk-*increasing* orders spend the budget the de-risk later needs. And
        ``fee_budget`` is keyed to the Gulf month, so once tripped it refused the close on every
        candle until the month turned — leaving only the per-position stop, in exactly the month
        that was expensive enough to exhaust the budget.

        0.10 of NAV separates the populations rather than splitting them: a full core close is
        0.30–0.40 of NAV at the core tier cap and always clears, while a rebalance trim is
        bounded by ``rebalance_band`` 0.05 and a take-profit rung is smaller still, so ordinary
        churn stays fully budgeted. The point of a size test is that the *next* new sell reason
        inherits the protection instead of silently missing it, which is how this arose.
        """
        cfg = self.cfg
        nav = max(ps.nav, _EPS)
        if is_risk_exit(exit_reason):
            return GateDecision(True, "ok", {"risk_exit": True})
        if abs(stake) >= cfg.derisk_exempt_pct * nav:
            return GateDecision(True, "ok", {"large_derisk": True})
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
        unlocked_month = ""
        if self.store.get("month_anchor_month") != month:
            # The month boundary re-anchors the measure AND releases what the measure
            # locked. Doing the two together is the whole fix: a lock re-anchored but
            # not released blocks forever, and a lock released but not re-anchored
            # re-arms on the very next tick (verified HIGH #10).
            self.store.set("month_anchor_month", month)
            self.store.set("month_anchor_nav", repr(ps.nav))
            unlocked_month = self._expire_monthly_lock(now)

        day_anchor = _fnum(self.store.get("day_anchor_nav"), ps.nav)
        month_anchor = _fnum(self.store.get("month_anchor_nav"), ps.nav)
        day_ret = ps.nav / day_anchor - 1 if day_anchor > 0 else 0.0
        month_ret = ps.nav / month_anchor - 1 if month_anchor > 0 else 0.0

        if month_ret <= -self.cfg.monthly_stop and not self._monthly_lock_active(now):
            self.store.set("monthly_locked", "1")
            self.store.set("monthly_locked_month", month)
            self.store.set("monthly_unlocked_utc", "")
            return LoopActions(flatten=True, flatten_reason="risk_stop_monthly",
                               monthly_lock=True)

        if day_ret <= -self.cfg.daily_stop and self.store.get("daily_stop_fired_date") != today:
            lock_until = now + timedelta(hours=self.cfg.daily_lock_hours)
            self.store.set("daily_stop_fired_date", today)
            self.store.set("locked_until", lock_until.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if self.cfg.daily_loss_response == "hold":
                # crisis-policy.md §0: the best-measured response to the trigger is to
                # sell NOTHING and stop buying. Fraction 0.0 is what flatten_pending and
                # reduce_pending read back as "nothing owed": the lock is the whole action.
                self.store.set("daily_stop_fraction", "0.0")
                return LoopActions(lock_until=lock_until)
            if self.cfg.daily_loss_response == "halve":
                # crisis-policy.md §0: the same -3% trigger, halve instead of flatten,
                # is +13.05% CAGR against -5.23%. The stop still locks entries for
                # daily_lock_hours; what changes is that the book is trimmed by half,
                # once, rather than sold.
                self.store.set("daily_stop_fraction", repr(DAILY_HALVE_FRACTION))
                return LoopActions(reduce=True, reduce_fraction=DAILY_HALVE_FRACTION,
                                   reduce_reason="risk_stop_daily", lock_until=lock_until)
            self.store.set("daily_stop_fraction", "1.0")
            return LoopActions(flatten=True, flatten_reason="risk_stop_daily",
                               lock_until=lock_until)
        return LoopActions(monthly_unlock=bool(unlocked_month),
                           unlocked_month=unlocked_month)

    # -- the monthly lock and its expiry ---------------------------------------

    def _monthly_lock_active(self, now: datetime) -> bool:
        """Is the monthly stop still in force at ``now``?

        The stored flag alone used to be the answer, and that made the monthly stop a
        permanent shutdown: nothing outside ``human_resume_monthly`` ever wrote it back
        to ``"0"``, so in a backtest — where no human exists — one -10% month ended the
        run. A real 2021-01→2026-09 SleeveA backtest stopped on 2021-05-22 with
        ``risk_stop_monthly`` and never traded again.

        The lock now carries the month it was measured over. Once the Gulf calendar
        month has turned past ``monthly_locked_month`` the anchor that produced the
        drawdown is gone and the lock goes with it. Read-side so it holds even before
        the next ``loop_tick`` has written the release; ``loop_tick`` then persists it.

        Fail-closed on a lock with no month recorded (only a hand-written row): a lock
        we cannot date is a lock. Comparison is lexicographic on ``YYYY-MM``, which is
        chronological, and uses ``<`` not ``!=`` so a clock that steps backwards — a
        re-run backtest sharing a store — cannot release a lock early.
        """
        if self.store.get("monthly_locked") != "1":
            return False
        locked_month = self.store.get("monthly_locked_month") or ""
        if not locked_month:
            return True
        return not (locked_month < _gulf_month(now))

    def _expire_monthly_lock(self, now: datetime) -> str:
        """Release a monthly lock whose month has ended. Returns the month released."""
        if self.store.get("monthly_locked") != "1":
            return ""
        locked_month = self.store.get("monthly_locked_month") or ""
        if locked_month and not (locked_month < _gulf_month(now)):
            return ""
        self.store.set("monthly_locked", "0")
        self.store.set("monthly_locked_month", "")
        self.store.set("monthly_unlocked_utc",
                       now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return locked_month

    def monthly_lock_status(self, now: datetime) -> dict[str, Any]:
        """Read-only view for the console and ``ops.lib.risk_resume.status``."""
        locked_month = self.store.get("monthly_locked_month") or ""
        return {
            "locked": self._monthly_lock_active(now),
            "locked_month": locked_month,
            "flag_set": self.store.get("monthly_locked") == "1",
            "expires_at_month_end": locked_month,
            "release": self.cfg.monthly_lock_release,
            "unlocked_utc": self.store.get("monthly_unlocked_utc") or "",
        }

    def _daily_locked(self, now: datetime) -> bool:
        raw = self.store.get("locked_until")
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        return now < until

    def flatten_pending(self, now: datetime) -> str | None:
        """Non-empty while a stop flatten is in force (drives custom_exit).

        ``now`` is the caller's clock — freqtrade's ``current_time`` — never the wall
        clock. The whole gate reads time that way (``check_entry`` uses ``ps.now``,
        ``trades_today(now)``, …) and this method used to be the one exception: it
        compared the stored ``locked_until`` against ``datetime.now(UTC)``. That failed
        OPEN in the two places where the two clocks differ. In a backtest every candle
        time is historical, so a lock written at a 2021 candle looked long expired
        against today's wall clock and the daily stop never held for a single simulated
        bar; and a test that pins a ``NOW`` saw the lock evaporate the moment real time
        passed it (``test_daily_stop_fires_flatten_and_lock``). The parameter is
        required on purpose: a safety read must not be able to fall back to a clock the
        caller is not using.
        """
        if self._monthly_lock_active(now):
            return "risk_stop_monthly"
        raw = self.store.get("daily_stop_fired_date")
        if raw and self._daily_locked(now) and self._daily_stop_is_flatten():
            return "risk_stop_daily"
        return None

    def _daily_stop_is_flatten(self) -> bool:
        """Did the daily stop that is in force ask for a flatten (not a halving)?

        Read from what the stop WROTE, not from today's config: a stop that fired as a
        flatten under one rendering must not turn into a halving because the config was
        regenerated an hour later, and vice versa. A row without the fraction (written
        before the key existed) is a flatten, which is what that gate did.
        """
        raw = self.store.get("daily_stop_fraction")
        if not raw:
            return True
        return _fnum(raw, 1.0) >= 1.0 - _EPS

    def reduce_pending(self, now: datetime) -> tuple[str, float] | None:
        """``(reason, fraction)`` while a HALVING daily stop is in force, else ``None``.

        The partial-exit twin of :meth:`flatten_pending`, and the read the adapter's
        ``adjust_trade_position`` consults: while the daily lock is live and the stop
        fired as a halving, every open trade owes one trim of ``fraction`` under
        ``reason`` (a risk exit, so churn and fee checks never block it). The adapter
        keeps the per-trade "already trimmed for this stop" mark; the gate only says
        whether a stop is in force. Same clock discipline as ``flatten_pending``: the
        caller's ``now``, never the wall clock.
        """
        if self._monthly_lock_active(now):
            return None  # the monthly flatten owns the book
        raw = self.store.get("daily_stop_fired_date")
        if not raw or not self._daily_locked(now) or self._daily_stop_is_flatten():
            return None
        fraction = _fnum(self.store.get("daily_stop_fraction"), DAILY_HALVE_FRACTION)
        if fraction <= _EPS:
            return None  # a `hold` stop: entries are locked and no trade owes a trim
        return "risk_stop_daily", min(max(fraction, 0.0), 1.0)

    def daily_stop_status(self, now: datetime) -> dict[str, Any]:
        """Read-only view for the console: is the daily stop in force, and as what."""
        fired = self.store.get("daily_stop_fired_date") or ""
        locked = bool(fired) and self._daily_locked(now)
        return {
            "locked": locked,
            "fired_date": fired,
            "locked_until": self.store.get("locked_until") or "",
            "response": self.cfg.daily_loss_response,
            "fraction": (_fnum(self.store.get("daily_stop_fraction"), 1.0)
                         if locked else 0.0),
        }

    def monthly_locked(self, now: datetime | None = None) -> bool:
        """The monthly stop as of ``now``. Without a clock this is the raw flag — the
        persisted state, which ``loop_tick`` writes back at the month boundary. Every
        enforcement path passes its own clock (``check_entry`` via ``ps.now``,
        ``flatten_pending(now)``), so the expiry can never be missed where it matters.
        """
        if now is None:
            return self.store.get("monthly_locked") == "1"
        return self._monthly_lock_active(now)

    # -- human-only resume -----------------------------------------------------

    def human_resume_monthly(self, ps: PortfolioState) -> ResumeResult:
        """Clear the monthly stop EARLY and re-anchor the month to today's NAV (HIGH #10).

        This is the LIVE human gate, not the only way out: ``loop_tick`` releases the
        lock by itself once the Gulf month it was measured over has ended. A human uses
        this to resume *before* that boundary.


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
        self.store.set("monthly_unlocked_utc", "")
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
            "open_positions": (float(len(self._held(ps))), float(cfg.max_open_positions)),
            "satellite_positions": (
                float(sum(1 for p in self._held(ps) if cfg.is_satellite(p))),
                float(cfg.max_satellite_positions),
            ),
            "satellite_gross": (
                sum(v for p, v in self._held(ps).items() if cfg.is_satellite(p)) / nav,
                cfg.max_satellite_gross,
            ),
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
