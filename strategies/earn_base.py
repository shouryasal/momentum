"""EarnBaseStrategy: the freqtrade adapter around the pure RiskGate. TIER 2.

Both sleeves inherit this. Every enforcement callback delegates to strategies/riskgate.py
and every *mechanic* to strategies/mechanics.py; this class only translates freqtrade's
objects (wallets, trades, orders) into the pure PortfolioState and journals what the gate
decided. It holds no limits of its own — everything comes from ``riskgate.json`` plus the
machine-local ``runtime-<sleeve>.json``.

Import note: inside the freqtrade container the strategy directory itself is on
sys.path (no package parent), host-side tests import the `strategies` package — hence
the dual import blocks.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freqtrade.strategy import IStrategy

try:
    from strategies import _journal
    from strategies import mechanics as mx
    from strategies import trend_state as ts
    from strategies.riskgate import (
        GateConfig,
        MemoryStateStore,
        PortfolioState,
        RiskGate,
        SqliteStateStore,
    )
except ImportError:  # in-container flat layout
    import _journal
    import mechanics as mx
    import trend_state as ts
    from riskgate import (
        GateConfig,
        MemoryStateStore,
        PortfolioState,
        RiskGate,
        SqliteStateStore,
    )

STRATEGY_VERSION = "earn-3"

#: The tier the trend ensemble gates: ``universe.core`` assets carry this tier in
#: ``riskgate.json`` (and, absent a snapshot, every capped asset does — see
#: ``riskgate.UniverseView``). Satellites are never touched by the ensemble.
_CORE_TIER = "core"

# Confirm-stage rejects for sizing limits mean cap_stake was bypassed upstream — that is
# a breach worth an alert; operational refusals are routine rejects.
_BREACH_CHECKS = {"weight_cap", "gross_cap", "usdt_floor", "order_notional",
                  "entries_per_trade", "nav_valid"}

# Exit reasons that arm the re-entry cooldown (spec section 9).
_COOLDOWN_EXITS = ("stop_loss", "trailing_stop_loss", "stoploss_on_exchange", "roi", "tp")

#: How often ``_audit_selfcheck`` may re-raise the same audit-trail alarm. Short enough
#: that an operator sees it inside one candle, long enough not to be a log storm.
_AUDIT_ALERT_INTERVAL_MIN = 15


def instrument_on() -> bool:
    """Is the diagnostic entry tap armed? ``EARN_INSTRUMENT_CSV=<path>`` arms it."""
    return bool(os.environ.get("EARN_INSTRUMENT_CSV"))


def instrument(kind: str, pair: str, when: Any, reason: str = "",
               value: float = 0.0) -> None:
    """Append one diagnostic row. OFF unless ``EARN_INSTRUMENT_CSV`` names a file.

    This exists for one question a backtest cannot otherwise answer: *why* an entry
    signal did not become a trade. Journaling is deliberately off in a backtest
    (``_journal_on = not _is_backtest``), and freqtrade's own "Rejected Entry signals"
    counts only ``confirm_trade_entry`` refusals — not the far commoner case of
    ``custom_stake_amount`` sizing the entry to zero, which looks like silence.

    Never raises and touches nothing when disarmed, so the live and TEST paths are
    byte-for-byte what they were.
    """
    path = os.environ.get("EARN_INSTRUMENT_CSV")
    if not path:
        return
    try:
        ts = when.isoformat() if hasattr(when, "isoformat") else str(when)
        line = f"{kind},{pair},{ts},{reason},{value!r}\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — a diagnostic may never break the loop
        pass


def _candles(hours: float, tf_hours: int) -> int:
    """A lock stated in HOURS, expressed in candles, never rounded away to nothing.

    Freqtrade's protections count candles, and the plain ``hours // tf_hours`` this
    replaces returns 0 whenever the configured lock is shorter than one candle — a
    2-hour ``stoploss_guard.lock_hours`` on the 4h timeframe silently meant *no lock at
    all*. The counterpart of the monthly-stop defect: a lock that dies before the
    condition that set it. At today's config every value is a whole multiple of the
    candle, so this changes no current number; it stops a future one from vanishing.
    """
    return max(int(float(hours) // max(int(tf_hours), 1)), 1)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return _aware(dt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AdjustPlan:
    """One candidate ``adjust_trade_position`` answer, computed WITHOUT side effects.

    ``stake`` is what the callback returns (positive = add, negative = a COST-BASIS
    trim); ``tag`` travels with the order as ``order.ft_order_tag``. ``effects`` are the
    writes — journal rows, per-trade add counters, cadence stamps — that may only happen
    once the candidate has WON the ``mechanics.ACTION_PRIORITY`` contest.

    That deferral is the whole point: ``_mechanics_plan`` builds every candidate before
    it knows which one is sent, so a candidate that loses must consume no budget. A
    pyramid add beaten by a take-profit rung used to increment ``pyramid_adds`` and
    stamp its cooldown for an order that was never submitted, and a losing SleeveB trim
    used to write a ``partial_exit`` journal row and burn the rebalance interval.
    """

    stake: float
    tag: str = ""
    effects: list[Callable[[], None]] = field(default_factory=list)

    def then(self, effect: Callable[[], None]) -> AdjustPlan:
        """Register a side effect to run only if this plan wins. Returns ``self``."""
        self.effects.append(effect)
        return self

    def commit(self) -> None:
        for effect in self.effects:
            effect()


class EarnBaseStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False
    process_only_new_candles = True
    position_adjustment_enable = True
    use_exit_signal = True
    startup_candle_count = 1320  # replaced per-instance from bounds['sleeve_a.trend.ma_days']

    # Spec section 9: entry/exit types are configurable; stop, emergency and force exits
    # are MARKET by invariant (a stop that rests as a limit is not a stop). Config JSON
    # must NOT carry order_types — it would overwrite this dict wholesale.
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
        "emergency_exit": "market",
        "force_exit": "market",
    }

    def __init__(self, config: dict) -> None:
        self.gate_cfg = GateConfig.load()
        cfg = self.gate_cfg
        self.mech: dict[str, Any] = dict(cfg.trading or {})
        sl_cfg = self.mech.get("stoploss") or {}

        self.timeframe = cfg.timeframe or self.timeframe
        self.startup_candle_count = mx.derived_startup_candles(
            cfg.bounds, self.timeframe, default=type(self).startup_candle_count)
        self.stoploss = mx.effective_fixed_stop(sl_cfg, cfg.stoploss_per_trade)
        self.minimal_roi = mx.roi_table(self.mech.get("take_profit"))
        self.use_custom_stoploss = bool(
            (sl_cfg.get("trailing") or {}).get("enabled")
            or (sl_cfg.get("atr") or {}).get("enabled")
        )
        self.order_types = dict(type(self).order_types)
        self.order_types["entry"] = str(
            (self.mech.get("order_types") or {}).get("entry", "limit"))
        self.order_types["exit"] = str(
            (self.mech.get("order_types") or {}).get("exit", "limit"))
        self.order_types["stoploss_on_exchange"] = mx.resolve_on_exchange(
            sl_cfg.get("on_exchange", "auto"), live=cfg.is_live)
        if self.order_types["stoploss_on_exchange"]:
            self.order_types["stoploss_on_exchange_interval"] = 60
        self.unfilledtimeout = {
            "entry": cfg.entry_unfilled_timeout_min,
            "exit": cfg.exit_unfilled_timeout_min,
            "exit_timeout_count": cfg.exit_timeout_count,
            "unit": "minutes",
        }

        super().__init__(config)
        runmode = getattr(config.get("runmode"), "value", str(config.get("runmode", "")))
        self._is_backtest = runmode in ("backtest", "hyperopt", "plot")
        self._journal_on = not self._is_backtest
        if self._is_backtest:
            store = MemoryStateStore()
            self.gate = RiskGate(
                cfg, store,
                flags_provider=lambda pair, now: (False, ""),
                staleness_provider=lambda now: 0.0,
                kill_provider=lambda: False,
                returns_provider=self._daily_returns,
            )
        else:
            store = SqliteStateStore(
                os.environ.get("EARN_JOURNAL_DB", "/freqtrade/journal/journal.db"),
                cfg.sleeve,
            )
            self.gate = RiskGate(cfg, store, returns_provider=self._daily_returns)
        self._pending_quotes: dict[str, tuple[str, float, float]] = {}
        self._params: dict = {}
        self._params_mtime: float = 0.0
        self._params_breached: str = ""
        #: Callbacks that MUST have produced a journal row, counted so the loop can tell
        #: "this bot is trading" from "this bot is idle" without a DB read.
        self._trade_events: int = 0
        self._audit_alert_at: datetime | None = None
        #: ``knowledge/state/trend.json`` parsed once per mtime; see :meth:`_trend_weight`.
        self._trend_cache: ts.TrendState | None = None
        #: Last trend-gate reason journalled per pair, so the journal gets one row per
        #: state change rather than one per candle.
        self._trend_gate_last: dict[str, str] = {}

    # ---------------------------------------------------------------- protections

    @property
    def protections(self) -> list[dict]:
        c = self.gate_cfg
        per_day = mx.candles_per_day(self.timeframe)
        tf_h = max(int(round(24.0 / per_day)) if per_day else 4, 1)
        return [
            # Already stated in candles, so only the "never zero" floor applies.
            {"method": "CooldownPeriod",
             "stop_duration_candles": max(int(c.cooldown_candles), 1)},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": _candles(c.stoploss_guard_window_h, tf_h),
                "trade_limit": c.stoploss_guard_count,
                "stop_duration_candles": _candles(c.stoploss_guard_lock_h, tf_h),
                "only_per_pair": False,
                "required_profit": 0.0,
            },
            {
                "method": "MaxDrawdown",
                "calculation_mode": "equity",
                "lookback_period_candles": c.protection_drawdown_lookback,
                "trade_limit": c.protection_drawdown_trade_limit,
                "max_allowed_drawdown": c.daily_stop,
                "stop_duration_candles": _candles(c.daily_lock_hours, tf_h),
            },
        ]

    # ---------------------------------------------------------------- portfolio

    def _last_price(self, pair: str) -> float:
        if not self._is_backtest:
            try:
                t = self.dp.ticker(pair)
                if t and t.get("last"):
                    return float(t["last"])
            except Exception:
                pass
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and len(df):
                return float(df["close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    #: Window for the gate's beta and correlation caps, in daily observations. 60 days is
    #: the window docs/design/wide-universe.md §2.1 measured every correlation and beta
    #: number on, so the limit and the evidence for it are computed the same way.
    RISK_WINDOW_DAYS = 60

    def _daily_returns(self, pair: str) -> list[float] | None:
        """Recent daily log-ish returns for one pair, for the gate's beta/corr checks.

        Read from the 1d informative frame the sleeves already populate — the gate asks
        for numbers, never for an opinion, and it never fetches anything itself. ``None``
        means "no history here", under which those two checks pass rather than blocking
        the book on a data gap the tier caps already bound.
        """
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            close = df["close_1d"] if "close_1d" in df else df["close"]
            series = close.dropna().drop_duplicates().tail(self.RISK_WINDOW_DAYS + 1)
            if len(series) < 3:
                return None
            return [float(x) for x in series.pct_change().dropna()]
        except Exception:  # noqa: BLE001 — a risk check may never break the loop
            return None

    def _open_trades(self) -> list:
        from freqtrade.persistence import Trade

        return list(Trade.get_trades_proxy(is_open=True))

    def _portfolio_state(self, now: datetime) -> PortfolioState:
        """Ledger NAV — the bot's own capital, never the whole exchange account.

        ``ledger_cash = starting_balance + closed profit + realized profit - Σ stake``;
        the part of a resting entry order whose stake that subtraction ALREADY removed
        is added back as ``reserved`` (see :meth:`_reserved_for` — for a trade that has
        already filled something it is nothing, because freqtrade recomputes
        ``stake_amount`` from filled orders only). NAV therefore does not move when an
        entry order is merely placed (verified HIGH #11) and is not inflated by one
        either, and USDT belonging to another sleeve or to the human never enters the
        number.
        """
        now = _aware(now)
        quote = self.config.get("stake_currency", "USDT")
        empty = {pair: 0.0 for pair in self.gate_cfg.pairs}
        try:
            from freqtrade.persistence import Trade

            start = float(self.wallets.get_starting_balance())
            positions = dict(empty)
            entries: dict[str, int] = {}
            pending: dict[str, float] = {}
            cost = 0.0
            reserved = 0.0
            realized = 0.0
            for trade in self._open_trades():
                pair = str(trade.pair)
                price = self._last_price(pair) or float(trade.open_rate or 0.0)
                positions[pair] = positions.get(pair, 0.0) + float(trade.amount or 0.0) * price
                cost += float(trade.stake_amount or 0.0)
                realized += float(getattr(trade, "realized_profit", 0.0) or 0.0)
                entries[pair] = entries.get(pair, 0) + self._entries_used(trade)
                resting = self._reserved_for(trade, price)
                reserved += resting
                # Per pair as well as in total: `positions` cannot see a resting entry order
                # (a brand-new trade has amount=0), so a sizing rule that reads only
                # `positions` re-buys a target it has already placed. See
                # `PortfolioState.committed`.
                pending[pair] = pending.get(pair, 0.0) + resting
            closed = float(Trade.get_total_closed_profit() or 0.0)
            ledger_cash = start + closed + realized - cost
            nav = ledger_cash + reserved + sum(positions.values())
            free = min(ledger_cash, float(self.wallets.get_free(quote) or 0.0))
        except (AttributeError, TypeError, ValueError) as exc:
            reason = f"{type(exc).__name__}"
            if self._journal_on:
                _journal.record_gate_decision(
                    self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", False,
                    f"nav_valid:{reason}", severity="breach",
                    strategy_version=STRATEGY_VERSION, run_id=self.gate_cfg.run_id or None,
                    action="reject",
                )
            return PortfolioState(nav=0.0, free_usdt=0.0, positions=dict(empty), now=now,
                                  valid=False, reason=reason)
        return PortfolioState(
            nav=nav, free_usdt=max(free, 0.0), positions=positions, now=now, valid=True,
            ledger_cash=ledger_cash, reserved_usdt=reserved, entries_used=entries,
            pending=pending,
        )

    @staticmethod
    def _entries_used(trade) -> int:
        for attr in ("nr_of_successful_entries", "nr_of_entries"):
            value = getattr(trade, attr, None)
            if isinstance(value, int) and value > 0:
                return value
        return 1

    @staticmethod
    def _reserved_for(trade, price: float) -> float:
        """USDT in this trade's open entry orders that ``ledger_cash`` HAS already deducted.

        ``_portfolio_state`` subtracts ``trade.stake_amount``. For a brand-new trade
        nothing has filled, ``recalc_trade_from_orders`` leaves the constructor's whole
        stake in place, and the entire resting notional was therefore deducted — adding
        it back is what keeps NAV flat when an order is merely placed. Once ANYTHING has
        filled, freqtrade recomputes ``stake_amount`` from filled orders only, so a
        resting position-adjustment order was never deducted and adding it back would
        INFLATE NAV (verified HIGH): ``loop_tick`` stamps the day/month anchor straight
        off that number, so an inflated anchor is a false daily flatten of the whole
        book. Hence ``min(resting, still-deducted)``: never more than the cash that
        actually left. Both halves are pinned in
        ``tests/contract/test_freqtrade_contract.py``.
        """
        resting = 0.0       # unfilled remainder of this trade's open entry orders
        filled_cost = 0.0   # cost basis of everything this trade's entry orders filled
        for order in (getattr(trade, "orders", None) or []):
            try:
                if order.ft_order_side != trade.entry_side:
                    continue
                amount = float(getattr(order, "safe_amount", 0.0) or 0.0)
                filled = float(getattr(order, "safe_filled", 0.0) or 0.0)
                rate = float(getattr(order, "safe_price", 0.0) or price)
                filled_cost += filled * rate
                if (getattr(order, "status", "") or "").lower() in ("open", "new",
                                                                    "partially_filled"):
                    resting += max(amount - filled, 0.0) * rate
            except (AttributeError, TypeError, ValueError):
                continue
        deducted = float(getattr(trade, "stake_amount", 0.0) or 0.0) - filled_cost
        return max(min(resting, deducted), 0.0)

    def _capture_quote(self, pair: str) -> tuple[str, float, float] | None:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self._is_backtest:
            try:
                ob = self.dp.orderbook(pair, 1)
                return (ts, float(ob["bids"][0][0]), float(ob["asks"][0][0]))
            except Exception:
                pass
        px = self._last_price(pair)
        return (ts, px, px) if px else None

    def _book(self, pair: str) -> tuple[float, float] | None:
        if self._is_backtest:
            return None
        try:
            ob = self.dp.orderbook(pair, 1)
            return float(ob["bids"][0][0]), float(ob["asks"][0][0])
        except Exception:
            return None

    # ---------------------------------------------------------------- params (tier 1)

    def _reload_params(self) -> None:
        """Reload the tier-1 params file and CLAMP every bounded key to riskgate bounds.

        An out-of-range or non-numeric value keeps the last good params and journals a
        ``params_out_of_bounds`` breach: a tier-1 writer can never widen a tier-2 limit
        from inside the container (verified HIGH #16).
        """
        p = Path(self.gate_cfg.config_dir) / f"params-sleeve-{self.gate_cfg.sleeve}.json"
        try:
            m = p.stat().st_mtime
            if m == self._params_mtime:
                return
            raw = json.loads(p.read_text()).get("params", {})
        except (OSError, json.JSONDecodeError):
            return  # keep last good params
        clamped, violations = mx.clamp_params(raw, self.gate_cfg.bounds)
        self._params_mtime = m
        if violations:
            detail = ",".join(sorted(violations))
            if detail != self._params_breached and self._journal_on:
                _journal.record_gate_decision(
                    self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", False,
                    f"params_out_of_bounds:{detail}", severity="breach",
                    checks={"params_out_of_bounds": False},
                    strategy_version=STRATEGY_VERSION, run_id=self.gate_cfg.run_id or None,
                    action="clamp",
                )
            self._params_breached = detail
            if self._params:
                return  # keep the last good params; the clamped ones are only a fallback
        else:
            self._params_breached = ""
        self._params = clamped

    # ---------------------------------------------------------------- trade custom data

    def _tdata(self, trade, key: str, default: Any = None) -> Any:
        try:
            value = trade.get_custom_data(key=key)
            if value is not None:
                return value
        except Exception:
            pass
        raw = self.gate.store.get(f"trade:{getattr(trade, 'id', 0)}:{key}")
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _set_tdata(self, trade, key: str, value: Any) -> None:
        try:
            trade.set_custom_data(key=key, value=value)
            return
        except Exception:
            pass
        self.gate.store.set(f"trade:{getattr(trade, 'id', 0)}:{key}", json.dumps(value))

    # ---------------------------------------------------------------- sleeve hooks

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        return proposed

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        return None

    def _sleeve_adjust(self, trade, ps: PortfolioState, current_time: datetime,
                       current_rate: float, current_profit: float) -> AdjustPlan | None:
        """Sleeve-specific position adjustment (scheduled DCA, rebalance). Lowest priority.

        Returns a PLAN, never a bare stake: this candidate may lose the priority
        contest, and then none of its side effects may happen.
        """
        return None

    def _regime_up(self, pair: str) -> bool:
        """Whether the trend regime is up — DCA's ``only_if_regime_up`` guard."""
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            return bool(df["regime_1d"].iloc[-1])
        except Exception:
            return False

    def _atr_value(self, pair: str) -> float | None:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            value = float(df["atr"].iloc[-1])
            return value if value > 0 else None
        except Exception:
            return None

    def _newest_proposal_at(self, pair: str) -> datetime | None:
        """When the newest usable proposal was produced (overrides the re-entry cooldown)."""
        return None

    # ---------------------------------------------------------------- trend ensemble

    def _is_core(self, pair: str) -> bool:
        """Is ``pair`` one of the ``universe.core`` assets the trend ensemble gates?"""
        asset = pair.split("/")[0]
        return self.gate_cfg.universe.tiers.get(asset, "") == _CORE_TIER

    def _trend_max_age_h(self) -> float:
        """``trading.trend_ensemble.max_age_hours`` when the config carries it, else the
        module default (one missed daily bar plus slack)."""
        try:
            raw = (self.mech.get("trend_ensemble") or {}).get("max_age_hours")
            return float(raw) if raw is not None else ts.DEFAULT_MAX_AGE_HOURS
        except (TypeError, ValueError):
            return ts.DEFAULT_MAX_AGE_HOURS

    def _trend_state(self) -> ts.TrendState:
        """The parsed ``knowledge/state/trend.json``, re-read only when its mtime moves."""
        path = ts.state_path(self.gate_cfg.knowledge_db)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = self._trend_cache
        if cached is None or cached.mtime != mtime or not cached.ok:
            cached = ts.load(path)
            self._trend_cache = cached
        return cached

    def _trend_weight(self, pair: str, now: datetime) -> ts.TrendWeight:
        """The ensemble's exposure weight for ``pair`` at ``now`` — the ENTRY GATE and the
        POSITION SCALE for core positions (``docs/design/dip-strategy.md`` §8.1 item 1).

        * a satellite is never gated: weight 1.0, reason ``not_core``;
        * a core asset gets the weight in ``trend.json`` when the file is present, parses,
          carries the asset with ``status: ok`` and its bar is fresh — otherwise 0.0 with
          the reason named (``trend_state.py`` lists them). Fail closed: a missing or stale
          signal is not a flat signal, and it is not a full one either.

        Applied in exactly two places, both on the BUY side: :meth:`custom_stake_amount`
        (a new entry) and :meth:`_gated_add` (every add). It never touches an exit — the
        stop, the ladder, ROI and the exit signal keep the whole exit path — and it never
        touches the risk gate, which still validates every scaled stake.
        """
        if not self._is_core(pair):
            return ts.TrendWeight(1.0, "not_core")
        return ts.weight_for(self._trend_state(), pair.split("/")[0], _aware(now),
                             self._trend_max_age_h())

    def _trend_scaled(self, pair: str, stake: float, ps: PortfolioState, *,
                      where: str, intent: str, tag: str) -> float:
        """``stake`` scaled by the ensemble weight for a core pair; 0.0 when the gate is shut.

        The scale is applied to the HEADROOM — ``weight × target × NAV − position`` — when
        the sleeve has a target for the pair, so repeated adds stop at the scaled target
        rather than each being scaled and still summing to the full one; a sleeve without a
        target (or a stake already inside the headroom) gets the plain multiple.
        """
        tw = self._trend_weight(pair, ps.now)
        self._journal_trend_gate(pair, tw, ps, where=where, intent=intent, stake=stake)
        if not tw.tradeable:
            instrument("trend_gate", pair, ps.now, f"{tw.reason}:{tag}", stake)
            return 0.0
        if tw.weight >= 1.0:
            return stake
        target_w = self._target_weight(pair)
        if target_w > 0:
            headroom = tw.weight * target_w * ps.nav - ps.committed(pair)
            scaled = max(min(stake, headroom), 0.0)
        else:
            scaled = stake * tw.weight
        instrument("trend_scale", pair, ps.now, f"w={tw.weight:.3f}:{tag}", scaled)
        return scaled

    def _journal_trend_gate(self, pair: str, tw: ts.TrendWeight, ps: PortfolioState, *,
                            where: str, intent: str, stake: float) -> None:
        """One journal row per pair per CHANGE of trend-gate state — never per candle.

        A shut gate is a sizing refusal, not a risk-gate breach (the risk gate never saw
        an order), so the row carries the ordinary ``reject`` severity and a reason that
        names plumbing (``trend_state_*``) or the market (``weight_zero``), so the Gate
        page and a healthcheck can tell them apart (§10.2 item 6).
        """
        key = f"{tw.reason}:{tw.weight:.3f}"
        if self._trend_gate_last.get(pair) == key:
            return
        self._trend_gate_last[pair] = key
        if not self._journal_on:
            return
        try:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, where, intent, tw.tradeable,
                f"trend_gate:{tw.reason}", side="buy",
                checks={"trend_gate": tw.tradeable}, proposed_stake=stake, nav=ps.nav,
                strategy_version=STRATEGY_VERSION, run_id=self.gate_cfg.run_id or None,
                action="allow" if tw.weight >= 1.0 else ("clamp" if tw.tradeable else "reject"),
            )
        except Exception:  # noqa: BLE001 — journaling never vetoes or approves an order
            pass

    # ---------------------------------------------------------------- gate helpers

    def _gated_add(self, pair: str, stake: float, ps: PortfolioState, *, trade=None,
                   tag: str = "") -> AdjustPlan | None:
        """EVERY positive adjustment goes through here: SIZE first, then enforce.

        ``cap_stake`` clamps the ask to the tightest sizing headroom (weight cap, gross
        cap, USDT floor, ``max_order_notional_pct``, remaining daily turnover) and the
        exchange filters put it on the LOT_SIZE/MIN_NOTIONAL grid; only then does
        ``check_entry`` run — on the number that will actually be sent. Gating the RAW
        ask instead (verified MEDIUM) turned a legal oversized proposal, e.g. a
        0.25*NAV SleeveB gap under a 0.2 ``max_order_notional_pct``, into a repeating
        ``order_notional`` *breach* alert and no order at all, while
        ``custom_stake_amount`` sized the identical first entry down without complaint.
        A sizing check can still only fail here if the cap was bypassed — which is
        exactly the breach the confirm stage is meant to shout about.

        Returns a PLAN: the journal row and the order tag are held back until the caller
        knows this candidate won the priority contest.
        """
        if stake <= 0:
            instrument("add_out", pair, ps.now, f"zero_stake:{tag}", 0.0)
            return None
        # The trend ensemble scales every add on a core pair before the gate sizes it,
        # and shuts the add entirely at weight zero (or on a missing/stale signal).
        stake = self._trend_scaled(pair, stake, ps, where="adjust_trade_position",
                                   intent="adjust", tag=tag)
        if stake <= 0:
            instrument("add_out", pair, ps.now, f"trend_gate:{tag}", 0.0)
            return None
        sized = self.gate.cap_stake(pair, stake, ps)
        capped = self._clamp_to_exchange(pair, sized)
        if sized > 0 and capped <= 0:
            instrument("add_out", pair, ps.now, f"exchange_limits:{tag}", stake)
            self._journal_adjust(pair, trade, False, "exchange_limits", None, stake, ps,
                                 action="reject", is_entry=True)
            return None
        decision = self.gate.check_entry(pair, capped, ps)
        if not decision.allowed:
            instrument("add_reject", pair, ps.now, f"{decision.reason}:{tag}", capped)
            self._journal_adjust(pair, trade, False, decision.reason, decision.checks,
                                 capped, ps, action="reject", is_entry=True)
            return None
        instrument("add_ok", pair, ps.now, tag, capped)
        reason = tag or "add"
        action = "clamp" if capped < stake - 1e-9 else "allow"
        plan = AdjustPlan(stake=capped, tag=tag)
        return plan.then(lambda: self._journal_adjust(
            pair, trade, True, reason, decision.checks, capped, ps, action=action,
            is_entry=True))

    def _exchange_filters(self, pair: str) -> mx.ExchangeFilters:
        limits = None
        try:
            market = self.dp._exchange.markets.get(pair) or {}
            limits = market.get("limits")
        except Exception:
            limits = None
        return mx.ExchangeFilters.from_limits(
            limits, min_notional_floor=self.gate_cfg.min_notional)

    def _clamp_to_exchange(self, pair: str, stake: float) -> float:
        """Clamp a stake onto the LOT_SIZE grid and MIN_NOTIONAL, or return 0."""
        if stake <= 0 or not (self.mech.get("exchange_limits") or {}).get(
                "respect_min_stake", True):
            return stake
        price = self._last_price(pair)
        if price <= 0:
            return stake
        return mx.clamp_stake(stake, price, self._exchange_filters(pair))

    def _journal_adjust(self, pair: str, trade, allowed: bool, reason: str,
                        checks: dict[str, bool] | None, stake: float, ps: PortfolioState,
                        *, action: str, is_entry: bool) -> None:
        if not self._journal_on:
            return
        failing = reason.split(":")[0]
        severity = "allow" if allowed else ("breach" if failing in _BREACH_CHECKS else "reject")
        # The direction is the CALLER's, not the stake's sign: a rejected take-profit rung
        # is journalled with a positive stake and is still a sell.
        side = self._order_side(getattr(trade, "trade_direction", None) or "long",
                                is_entry=is_entry, where="adjust_trade_position")
        _journal.record_gate_decision(
            self.gate_cfg.sleeve, pair, "adjust_trade_position", "adjust", allowed, reason,
            side=side,
            severity=severity, checks=checks, proposed_stake=stake, nav=ps.nav,
            gross_exposure=ps.gross_exposure, strategy_version=STRATEGY_VERSION,
            run_id=self.gate_cfg.run_id or None, action=action,
            trade_id=getattr(trade, "id", None),
        )

    # ---------------------------------------------------------------- audit trail

    def _order_side(self, side: str | None, *, is_entry: bool, where: str) -> str | None:
        """freqtrade's ``side`` argument as the EXCHANGE order side the journal stores.

        The entry callbacks are handed the POSITION side (``long``/``short``); every
        journal ``side`` column CHECKs ``IN ('buy','sell')``. This is the single
        conversion point — see :func:`strategies.mechanics.order_side`. An unrecognised
        value journals NULL and raises an incident rather than killing the row.
        """
        mapped = mx.order_side(side, is_entry=is_entry)
        if mapped is None and self._journal_on:
            _journal.alert(
                "freqtrade_side_unknown",
                f"{where}: freqtrade passed side={side!r}, which is neither a position "
                f"side {mx.POSITION_SIDES} nor an order side {mx.ORDER_SIDES} — "
                f"journalling NULL; the freqtrade contract test needs re-pinning",
            )
        return mapped

    def _audit_selfcheck(self, now: datetime) -> None:
        """Fail loudly when this bot is trading but its journal rows are not arriving.

        The journal writers swallow their exceptions on purpose (journalling must never
        veto an order) — which is exactly how 122 rejected writes stayed invisible for
        eight hours while both sleeves held open dry-run positions. This runs every bot
        loop and turns either symptom into an ``incidents`` row plus the ``.write_failed``
        health marker ops/healthcheck.py alerts on:

        * a swallowed write (``failed > 0``) — something raised and was eaten; and
        * an empty audit trail (trade callbacks ran, ``ok == 0``) — nothing raised at all
          and still not one row landed.
        """
        if not self._journal_on:
            return
        st = _journal.stats()
        failed, ok = int(st.get("failed") or 0), int(st.get("ok") or 0)
        if failed:
            problem = (f"{failed} journal write(s) swallowed since "
                       f"{st.get('first_failure_utc')} — last error: {st.get('last_error')}")
        elif self._trade_events and not ok:
            problem = (f"{self._trade_events} trade callback(s) ran and not one journal "
                       f"row was written — the audit trail is empty")
        else:
            return
        last = self._audit_alert_at
        if last is not None and (
                _aware(now) - last).total_seconds() < _AUDIT_ALERT_INTERVAL_MIN * 60:
            return
        self._audit_alert_at = _aware(now)
        _journal.alert("audit_trail_missing",
                       f"sleeve {self.gate_cfg.sleeve} is taking trades without an audit "
                       f"trail: {problem}", dedupe=False)

    # ---------------------------------------------------------------- callbacks

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._audit_selfcheck(current_time)
        self._reload_params()
        ps = self._portfolio_state(current_time)
        actions = self.gate.loop_tick(ps)
        if actions.monthly_unlock:
            # The Gulf month the drawdown was measured over has ended, so the stop it
            # armed has ended with it. Journalled with the same weight as the stop, and
            # any freqtrade pair locks the flatten left behind go with it: they are the
            # daily stop's timed locks and the month boundary is always past them.
            self._release_risk_pair_locks(current_time)
            if self._journal_on:
                _journal.record_gate_decision(
                    self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", True,
                    f"risk_stop_monthly_expired:{actions.unlocked_month}",
                    severity="allow", nav=ps.nav, gross_exposure=ps.gross_exposure,
                    strategy_version=STRATEGY_VERSION,
                    run_id=self.gate_cfg.run_id or None, action="allow",
                )
        if not actions.flatten:
            return
        if self._journal_on:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", False,
                actions.flatten_reason, severity="breach", nav=ps.nav,
                gross_exposure=ps.gross_exposure, strategy_version=STRATEGY_VERSION,
                run_id=self.gate_cfg.run_id or None, action="reject",
            )
        if self._is_backtest:
            return
        # HIGH #10: only the DAILY stop takes a pair lock, and it is a TIMED one that
        # expires with the stop (`daily_stop_lock_hours` from the moment it fired). The
        # monthly stop takes none — it is held by the gate's own dated `monthly_locked`
        # flag, which the Gulf month boundary releases — so neither a human resume nor
        # the next month is fighting a lock that outlived its reason.
        if actions.lock_until is None:
            return
        for pair in self.gate_cfg.pairs:
            try:
                self.lock_pair(pair, until=actions.lock_until, reason=actions.flatten_reason)
            except Exception:
                pass

    def _release_risk_pair_locks(self, now: datetime) -> None:
        """Drop any still-live freqtrade pair lock this sleeve's risk stops created.

        Best effort and never raises into the loop: the lock list is freqtrade's, and a
        missing API on an older release must not stop the month from turning.
        """
        if self._is_backtest:
            return
        # Only OUR reasons: CooldownPeriod / StoplossGuard / MaxDrawdown locks are
        # freqtrade's, carry their own candle-counted expiry, and are not ours to clear.
        for reason in ("risk_stop_daily", "risk_stop_monthly"):
            try:
                self.unlock_reason(reason)
            except Exception:  # noqa: BLE001 — a lock we cannot drop is not fatal
                pass

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime,
                            entry_tag: str | None, side: str, **kwargs) -> bool:
        self._trade_events += 1
        ps = self._portfolio_state(current_time)
        quote = self._capture_quote(pair)
        stake = amount * rate
        d = self.gate.check_entry(pair, stake, ps)
        instrument("confirm_ok" if d.allowed else "confirm_reject", pair, current_time,
                   d.reason, stake)
        if d.allowed:
            self._pending_quotes[pair] = quote
        if self._journal_on:
            failing = d.reason.split(":")[0]
            severity = "allow" if d.allowed else (
                "breach" if failing in _BREACH_CHECKS else "reject")
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "confirm_trade_entry", "entry", d.allowed,
                d.reason,
                side=self._order_side(side, is_entry=True, where="confirm_trade_entry"),
                severity=severity, checks=d.checks,
                proposed_stake=stake, quote=quote, nav=ps.nav,
                gross_exposure=ps.gross_exposure, strategy_version=STRATEGY_VERSION,
                run_id=self.gate_cfg.run_id or None,
                action="allow" if d.allowed else "reject",
            )
        return d.allowed

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        self._trade_events += 1
        ps = self._portfolio_state(current_time)
        quote = self._capture_quote(pair)
        self._pending_quotes[pair] = quote
        d = self.gate.check_discretionary_exit(pair, amount * rate, ps, exit_reason)
        if self._journal_on:
            # freqtrade passes NO `side` to this callback (pinned in the contract test):
            # the exit's order side comes off the trade — `exit_side` is already buy/sell,
            # `trade_direction` is the long/short fallback both of which map to 'sell' for
            # a spot long. Hard-coding "sell" would silently lie the day shorts appear.
            exit_side = self._order_side(
                getattr(trade, "exit_side", None) or getattr(trade, "trade_direction", "long"),
                is_entry=False, where="confirm_trade_exit")
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "confirm_trade_exit", "exit", d.allowed,
                exit_reason if d.allowed else f"{d.reason}:{exit_reason}", side=exit_side,
                severity="allow" if d.allowed else "reject", checks=d.checks, quote=quote,
                nav=ps.nav, strategy_version=STRATEGY_VERSION,
                run_id=self.gate_cfg.run_id or None,
                action="allow" if d.allowed else "reject",
                trade_id=getattr(trade, "id", None),
            )
        return d.allowed

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        ps = self._portfolio_state(current_time)
        desired = self._desired_stake(pair, ps, proposed_stake, entry_tag)
        if os.environ.get("EARN_DRILL") == "oversize":
            # Bad-order drill: bypass the cap so the confirm-stage gate must reject it.
            return 0.9 * ps.nav
        if desired <= 0:
            instrument("sized_out", pair, current_time,
                       f"desired_zero:{entry_tag or ''}", 0.0)
            return 0.0
        # The trend ensemble is the ENTRY GATE and POSITION SCALE for a core pair: no
        # entry at weight zero (or without a fresh signal), the stake scaled otherwise.
        desired = self._trend_scaled(pair, desired, ps, where="custom_stake_amount",
                                     intent="entry", tag=entry_tag or "")
        if desired <= 0:
            instrument("sized_out", pair, current_time,
                       f"trend_gate:{entry_tag or ''}", 0.0)
            return 0.0
        if max_stake:
            desired = min(desired, max_stake)
        capped = self.gate.cap_stake(pair, desired, ps)
        capped = self._clamp_to_exchange(pair, capped)
        if instrument_on():
            instrument("sized" if capped > 0 else "sized_out", pair, current_time,
                       f"cap_stake:{entry_tag or ''}" if capped <= 0 else (entry_tag or ""),
                       capped)
        if self._journal_on and 0 < capped < desired:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "custom_stake_amount", "entry", True,
                "clamped",
                side=self._order_side(side, is_entry=True, where="custom_stake_amount"),
                checks={"clamped": True}, proposed_stake=desired,
                nav=ps.nav, strategy_version=STRATEGY_VERSION,
                run_id=self.gate_cfg.run_id or None, action="clamp",
            )
        return capped

    def custom_entry_price(self, pair: str, trade, current_time: datetime,
                           proposed_rate: float, entry_tag: str | None, side: str,
                           **kwargs) -> float:
        book = self._book(pair)
        if book is None:
            return proposed_rate
        bid, ask = book
        price = mx.limit_price(self.mech.get("entry_price"), bid, ask, is_entry=True,
                               fallback=proposed_rate)
        return mx.clamp_price(price, self._exchange_filters(pair).tick_size, side="bid")

    def custom_exit_price(self, pair: str, trade, current_time: datetime,
                          proposed_rate: float, current_profit: float,
                          exit_tag: str | None, **kwargs) -> float:
        book = self._book(pair)
        if book is None:
            return proposed_rate
        bid, ask = book
        if exit_tag and mx.is_risk_exit(exit_tag):
            # Marketable limit: cross the spread so the flatten fills immediately;
            # unfilled after exit_timeout_count replacements -> emergency_exit (market).
            return bid * (1 - self.gate_cfg.cross_ticks_buffer)
        price = mx.limit_price(self.mech.get("exit_price"), bid, ask, is_entry=False,
                               fallback=proposed_rate)
        return mx.clamp_price(price, self._exchange_filters(pair).tick_size, side="ask")

    def custom_stoploss(self, pair: str, trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool = False,
                        **kwargs) -> float | None:
        """Trailing / ATR stop, never looser than the fixed stop (spec section 9)."""
        sl_cfg = self.mech.get("stoploss") or {}
        open_rate = float(getattr(trade, "open_rate", 0.0) or 0.0)
        max_rate = float(getattr(trade, "max_rate", 0.0) or 0.0)
        max_profit = (max_rate / open_rate - 1.0) if open_rate > 0 and max_rate > 0 else \
            float(current_profit)
        max_profit = max(max_profit, float(current_profit))
        return mx.custom_stoploss_ratio(
            sl_cfg, current_profit=float(current_profit), max_profit=max_profit,
            atr_value=self._atr_value(pair), open_rate=open_rate,
            current_rate=float(current_rate), fixed_ceiling=self.gate_cfg.stoploss_per_trade,
        )

    def custom_exit(self, pair: str, trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        reason = self.gate.flatten_pending(_aware(current_time))
        if reason:
            return reason
        return self._custom_exit_extra(pair, trade)

    def check_entry_timeout(self, pair: str, trade, order, current_time: datetime,
                            **kwargs) -> bool:
        if self.gate._kill() or self.gate.flatten_pending(_aware(current_time)):
            return True  # cancel open entry orders under KILL or an active stop
        return bool(self.mech.get("reprice_on_timeout", False))

    def check_exit_timeout(self, pair: str, trade, order, current_time: datetime,
                           **kwargs) -> bool:
        # An unfilled exit is replaced by freqtrade; repricing is the configured default
        # and a risk flatten always wants the replacement.
        if self.gate.flatten_pending(_aware(current_time)):
            return True
        return bool(self.mech.get("reprice_on_timeout", False))

    # ---------------------------------------------------------------- adjust dispatcher

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit,
                              min_stake, max_stake, current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        """The single dispatcher (spec section 9). Sleeves override ``_sleeve_adjust``.

        Priority per trade per candle: flatten pending → TP ladder → DCA/pyramid →
        sleeve rebalance. Exactly one action is returned; ``None`` means do nothing.

        Returns freqtrade's ``(stake, order_tag)`` pair (``_adjust_trade_position_internal``
        unpacks a tuple) so the winner's tag rides on the order it created —
        ``order.ft_order_tag`` — instead of being stashed per pair before the winner is
        known. Only adds carry a tag; an exit passes ``""`` so freqtrade keeps its own
        ``partial_exit`` reason.
        """
        plan = self._mechanics_plan(trade, current_time, current_rate, current_profit,
                                    min_stake, max_stake)
        if plan is None:
            return None, ""
        return plan.stake, plan.tag

    def _mechanics_plan(self, trade, current_time, current_rate, current_profit,
                        min_stake, max_stake) -> AdjustPlan | None:
        """Build every candidate PURELY, pick one, then commit ONLY the winner's effects."""
        now = _aware(current_time)
        if self.gate.flatten_pending(now):
            return None  # custom_exit owns the flatten; never add or trim beside it
        ps = self._portfolio_state(now)
        if not ps.valid:
            return None

        actions = mx.ActionSet()
        actions.offer("take_profit",
                      self._ladder_action(trade, ps, current_rate, current_profit, min_stake))
        actions.offer("add",
                      self._add_action(trade, ps, now, current_rate, current_profit, min_stake))
        actions.offer("rebalance",
                      self._sleeve_adjust(trade, ps, now, current_rate, current_profit))

        chosen = actions.choose()
        if chosen is None:
            return None
        plan: AdjustPlan = chosen[1]
        plan.commit()
        return plan

    def _mechanics_adjust(self, trade, current_time, current_rate, current_profit,
                          min_stake, max_stake) -> float | None:
        """The chosen stake alone, for callers that do not care about the order tag."""
        plan = self._mechanics_plan(trade, current_time, current_rate, current_profit,
                                    min_stake, max_stake)
        return None if plan is None else plan.stake

    # -- exit sizing ------------------------------------------------------------

    @staticmethod
    def _cost_basis_exit(trade, sell_value: float, position_value: float, *,
                        full_exit: bool = False) -> float | None:
        """Turn a MARKET-VALUE trim into the cost-basis stake freqtrade expects.

        ``adjust_trade_position``'s negative return is a fraction of
        ``trade.stake_amount``: freqtradebot sells
        ``|stake| * trade.amount / trade.stake_amount`` base units, and
        ``trade.stake_amount`` is the trade's COST BASIS (``amount * open_rate``), not
        its market value — both pinned in ``tests/contract/test_freqtrade_contract.py``.
        Handing freqtrade a market value therefore oversells by exactly the open profit
        (a 50% rung sells 50% too much), and the ladder's "full exit" asks for more base
        than the position holds, which freqtradebot silently declines because
        ``remaining`` goes negative — the intended flatten never happens at all.
        """
        basis = float(getattr(trade, "stake_amount", 0.0) or 0.0)
        if basis <= 0:
            return None
        if full_exit:
            return -basis
        if position_value <= 0:
            return None
        fraction = min(max(abs(float(sell_value)) / float(position_value), 0.0), 1.0)
        if fraction <= 0:
            return None
        return -(fraction * basis)

    # -- take profit ------------------------------------------------------------

    def _ladder_action(self, trade, ps: PortfolioState, current_rate: float,
                       current_profit: float,
                       min_stake: float | None = None) -> AdjustPlan | None:
        ladder = (self.mech.get("take_profit") or {}).get("ladder") or []
        if not ladder:
            return None
        pair = str(trade.pair)
        limits = self.mech.get("exchange_limits") or {}
        fired = list(self._tdata(trade, "tp_rungs", []) or [])
        position_value = float(trade.amount or 0.0) * float(current_rate or 0.0)
        decision = mx.ladder_step(
            ladder, fired, current_profit=float(current_profit),
            position_value=position_value,
            min_exit_stake=max(self.gate_cfg.min_notional, float(min_stake or 0.0)),
            dust_stake=self.gate_cfg.dust_weight * max(ps.nav, 1e-9),
            full_exit_below_min=bool(limits.get("full_exit_below_min", True)),
            filters=self._exchange_filters(pair) if limits.get("respect_min_stake", True)
            else None,
            price=float(current_rate or 0.0),
        )
        if decision.rung is None:
            return None
        if not decision.acts:
            # The documented burn: a rung whose slice is under min_exit_stake is marked
            # fired here so it cannot block the ladder forever.
            self._set_tdata(trade, "tp_rungs", list(decision.fired))
            self._journal_adjust(pair, trade, False, f"tp_skip:{decision.reason}", None,
                                 0.0, ps, action="reject", is_entry=False)
            return None
        gate = self.gate.check_discretionary_exit(pair, decision.sell_stake, ps,
                                                 f"tp{decision.rung + 1}")
        if not gate.allowed:
            # The rung is deliberately NOT persisted as fired (verified HIGH): a
            # churn/turnover/fee refusal must not lose the profit level. fee_budget is a
            # MONTHLY counter, so burning here would silently cost every remaining rung
            # for the rest of the month.
            self._journal_adjust(pair, trade, False, gate.reason, gate.checks,
                                 decision.sell_stake, ps, action="reject", is_entry=False)
            return None
        stake = self._cost_basis_exit(trade, decision.sell_stake, position_value,
                                      full_exit=decision.full_exit)
        if stake is None:
            self._journal_adjust(pair, trade, False, "tp_skip:no_cost_basis", gate.checks,
                                 decision.sell_stake, ps, action="reject", is_entry=False)
            return None
        plan = AdjustPlan(stake=stake)
        plan.then(lambda: self._set_tdata(trade, "tp_rungs", list(decision.fired)))
        return plan.then(lambda: self._journal_adjust(
            pair, trade, True, decision.reason, gate.checks, -decision.sell_stake, ps,
            action="partial_exit", is_entry=False))

    # -- adds -------------------------------------------------------------------

    def _add_action(self, trade, ps: PortfolioState, now: datetime, current_rate: float,
                    current_profit: float, min_stake: float | None) -> AdjustPlan | None:
        pair = str(trade.pair)
        first_stake = float(self._tdata(trade, "first_stake", 0.0) or 0.0)
        if first_stake <= 0:
            first_stake = float(getattr(trade, "stake_amount", 0.0) or 0.0) / max(
                self._entries_used(trade), 1)
            if first_stake > 0:
                self._set_tdata(trade, "first_stake", first_stake)
        entries_used = self._entries_used(trade)
        dca_used = int(self._tdata(trade, "dca_adds", 0) or 0)
        pyr_used = int(self._tdata(trade, "pyramid_adds", 0) or 0)

        decision = mx.dca_add(
            self.mech.get("dca"), adds_used=dca_used, current_profit=float(current_profit),
            first_stake=first_stake, now=now,
            last_add=_parse(self._tdata(trade, "last_dca_add")),
            regime_up=self._regime_up(pair),
            max_entries=self.gate_cfg.max_entries_per_trade, entries_used=entries_used,
        )
        kind_key = "dca_adds"
        stamp_key = "last_dca_add"
        if not decision.acts:
            decision = mx.pyramid_add(
                self.mech.get("pyramid"), adds_used=pyr_used,
                current_profit=float(current_profit), first_stake=first_stake, now=now,
                last_add=_parse(self._tdata(trade, "last_pyramid_add")),
                max_entries=self.gate_cfg.max_entries_per_trade, entries_used=entries_used,
            )
            kind_key, stamp_key = "pyramid_adds", "last_pyramid_add"
        if not decision.acts:
            return None

        stake = self._clamp_add_to_target(pair, decision.stake, ps)
        plan = self._gated_add(pair, stake, ps, trade=trade, tag=decision.tag)
        if plan is None:
            return None
        used = dca_used if kind_key == "dca_adds" else pyr_used
        # Deferred: the add budget and its cooldown may only be consumed by an order
        # that is actually submitted, i.e. by the winner of the priority contest.
        plan.then(lambda: self._set_tdata(trade, kind_key, used + 1))
        return plan.then(lambda: self._set_tdata(trade, stamp_key, _iso(now)))

    def _clamp_add_to_target(self, pair: str, stake: float, ps: PortfolioState) -> float:
        """In target-weight sizing an add may never push past ``target_w * nav``."""
        if str(self.mech.get("sizing_mode", "target_weight")) != "target_weight":
            return stake
        target_w = self._target_weight(pair)
        if target_w <= 0:
            return 0.0
        headroom = target_w * ps.nav - ps.committed(pair)
        return max(min(stake, headroom), 0.0)

    def _target_weight(self, pair: str) -> float:
        """The sleeve's current target weight for ``pair`` (0 disables target clamping)."""
        return self.gate_cfg.weight_caps.get(pair, 0.0)

    # ---------------------------------------------------------------- fills

    def _fill_fee(self, trade, order, *, is_entry: bool,
                  notional: float) -> tuple[float, str, float]:
        """``(fee_amount, fee_currency, fee_in_quote)`` for ONE fill.

        freqtrade's ``Order`` has no ``fee_cost``/``fee_currency``: reading them raised
        AttributeError on every real fill, and freqtrade swallows it
        (``strategy_safe_wrapper(..., supress_error=True)``), so the fills table stayed
        empty and every churn/turnover/fee counter the gate reads back stayed at zero.
        What the model actually carries — pinned in
        ``tests/contract/test_freqtrade_contract.py`` — is ``Order.safe_fee_base``, the
        fee taken out of the BASE asset in base units, plus the per-trade fee RATE
        (``Trade.fee_open`` / ``Trade.fee_close``) and its currency; ``fee_open_cost`` is
        a trade-level total, not this fill's share.
        """
        quote_ccy = str(self.config.get("stake_currency", "USDT") or "USDT")
        try:
            base_fee = float(getattr(order, "safe_fee_base", 0.0) or 0.0)
            price = float(getattr(order, "safe_price", 0.0) or 0.0)
            if base_fee > 0:
                base_ccy = str(getattr(trade, "pair", "") or "").split("/")[0]
                return base_fee, base_ccy or quote_ccy, base_fee * price
            rate = getattr(trade, "fee_open" if is_entry else "fee_close", 0.0)
            fee_quote = abs(float(notional)) * float(rate or 0.0)
            ccy = getattr(trade, "fee_open_currency" if is_entry else "fee_close_currency", "")
            return fee_quote, str(ccy or quote_ccy), fee_quote
        except (AttributeError, TypeError, ValueError):
            return 0.0, quote_ccy, 0.0

    def order_filled(self, pair: str, trade, order, current_time: datetime, **kwargs) -> None:
        """Enforcement counters and cadence stamps FIRST, journalling last.

        freqtrade calls this through ``strategy_safe_wrapper(..., supress_error=True)``,
        so anything raised here is swallowed without a trace. The counters below are what
        the gate reads back for ``orders_per_day`` / ``turnover_day`` / ``fee_budget`` /
        ``max_trades_per_day``, and the stamps are what arm the DCA interval and the
        re-entry cooldown — none of that may ever depend on a journal write succeeding.
        """
        self._trade_events += 1
        quote = self._pending_quotes.pop(pair, None)
        is_entry = order.ft_order_side == trade.entry_side
        now = _aware(current_time)
        amount = float(getattr(order, "safe_filled", 0.0) or 0.0)
        price = float(getattr(order, "safe_price", 0.0) or 0.0)
        notional = amount * price
        fee_amount, fee_currency, fee_usdt = self._fill_fee(
            trade, order, is_entry=is_entry, notional=notional)
        exit_reason = str(getattr(trade, "exit_reason", "") or "")
        order_tag = str(getattr(order, "ft_order_tag", "") or "")

        self.gate.record_order_fill(now, notional=notional, fee_usdt=fee_usdt,
                                    risk_exit=(not is_entry) and mx.is_risk_exit(exit_reason))
        if is_entry:
            # One TRADE, not one order: counting every entry fill against
            # max_trades_per_day let a single trade that averaged down three times
            # exhaust the whole day's trade budget for every other pair.
            if self._entries_used(trade) <= 1:
                self.gate.record_entry_fill(now)
            if order_tag in ("scheduled_dca", "dca"):
                # Scheduled DCA is stamped on the FILL, not on submission (spec section 9),
                # so a cancelled or unfilled chunk does not consume the interval — and off
                # THIS order's own tag, so a pyramid fill on the same pair cannot consume
                # the week's calendar DCA.
                self.gate.store.set(f"last_dca_fill_{pair}", _iso(now))
            if not self._tdata(trade, "first_stake", None):
                self._set_tdata(trade, "first_stake", notional)
        else:
            self._stamp_exit(pair, trade, exit_reason, now)

        if not self._journal_on:
            return
        try:
            oid = _journal.record_order(
                self.gate_cfg.sleeve, pair, order.ft_order_side, order.order_type or "limit",
                amount, price, "filled",
                ft_trade_id=trade.id, ft_order_id=str(order.order_id),
                mode=self.gate_cfg.mode, run_id=self.gate_cfg.run_id or None,
            )
            _journal.record_fill(
                self.gate_cfg.sleeve, pair, order.ft_order_side, amount, price,
                order_id=oid, fee_amount=fee_amount, fee_currency=fee_currency,
                ft_order_id=str(order.order_id), quote=quote,
                mode=self.gate_cfg.mode, run_id=self.gate_cfg.run_id or None,
            )
        except Exception as exc:  # noqa: BLE001 — never let the journal stop enforcement
            print(f"earn: journal write failed for {pair} fill: {exc!r}", file=sys.stderr)

    def _stamp_exit(self, pair: str, trade, exit_reason: str, now: datetime) -> None:
        """Arm the re-entry cooldown after a stop / ROI / take-profit exit."""
        reason = (exit_reason or "").lower()
        if not reason.startswith(_COOLDOWN_EXITS):
            return
        self.gate.store.set(f"stopped_{pair}", _iso(now))

    def _reentry_blocked(self, pair: str, now: datetime) -> bool:
        cooldown = float((self.mech.get("stoploss") or {}).get("reentry_cooldown_hours", 0) or 0)
        return mx.reentry_blocked(
            now=_aware(now), stopped_at=_parse(self.gate.store.get(f"stopped_{pair}")),
            cooldown_hours=cooldown, proposal_at=self._newest_proposal_at(pair),
        )
