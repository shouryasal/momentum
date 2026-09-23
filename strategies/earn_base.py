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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freqtrade.strategy import IStrategy

try:
    from strategies import _journal
    from strategies import mechanics as mx
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
    from riskgate import (
        GateConfig,
        MemoryStateStore,
        PortfolioState,
        RiskGate,
        SqliteStateStore,
    )

STRATEGY_VERSION = "earn-2"

# Confirm-stage rejects for sizing limits mean cap_stake was bypassed upstream — that is
# a breach worth an alert; operational refusals are routine rejects.
_BREACH_CHECKS = {"weight_cap", "gross_cap", "usdt_floor", "order_notional",
                  "entries_per_trade", "nav_valid"}

# Exit reasons that arm the re-entry cooldown (spec section 9).
_COOLDOWN_EXITS = ("stop_loss", "trailing_stop_loss", "stoploss_on_exchange", "roi", "tp")


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
            )
        else:
            store = SqliteStateStore(
                os.environ.get("EARN_JOURNAL_DB", "/freqtrade/journal/journal.db"),
                cfg.sleeve,
            )
            self.gate = RiskGate(cfg, store)
        self._pending_quotes: dict[str, tuple[str, float, float]] = {}
        self._pending_add_tag: dict[str, str] = {}
        self._params: dict = {}
        self._params_mtime: float = 0.0
        self._params_breached: str = ""

    # ---------------------------------------------------------------- protections

    @property
    def protections(self) -> list[dict]:
        c = self.gate_cfg
        per_day = mx.candles_per_day(self.timeframe)
        tf_h = max(int(round(24.0 / per_day)) if per_day else 4, 1)
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": c.cooldown_candles},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": c.stoploss_guard_window_h // tf_h,
                "trade_limit": c.stoploss_guard_count,
                "stop_duration_candles": c.stoploss_guard_lock_h // tf_h,
                "only_per_pair": False,
                "required_profit": 0.0,
            },
            {
                "method": "MaxDrawdown",
                "calculation_mode": "equity",
                "lookback_period_candles": c.protection_drawdown_lookback,
                "trade_limit": c.protection_drawdown_trade_limit,
                "max_allowed_drawdown": c.daily_stop,
                "stop_duration_candles": c.daily_lock_hours // tf_h,
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

    def _open_trades(self) -> list:
        from freqtrade.persistence import Trade

        return list(Trade.get_trades_proxy(is_open=True))

    def _portfolio_state(self, now: datetime) -> PortfolioState:
        """Ledger NAV — the bot's own capital, never the whole exchange account.

        ``ledger_cash = starting_balance + closed profit + realized profit - Σ stake``;
        a resting entry order has already had its stake deducted, so its unfilled
        remainder is added back as ``reserved``. NAV therefore does not move when an
        entry order is merely placed (verified HIGH #11), and USDT belonging to another
        sleeve or to the human never enters the number.
        """
        now = _aware(now)
        quote = self.config.get("stake_currency", "USDT")
        empty = {pair: 0.0 for pair in self.gate_cfg.pairs}
        try:
            from freqtrade.persistence import Trade

            start = float(self.wallets.get_starting_balance())
            positions = dict(empty)
            entries: dict[str, int] = {}
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
                reserved += self._reserved_for(trade, price)
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
        """USDT still sitting in this trade's *open entry* orders."""
        total = 0.0
        for order in (getattr(trade, "orders", None) or []):
            try:
                if order.ft_order_side != trade.entry_side:
                    continue
                if (getattr(order, "status", "") or "").lower() not in ("open", "new",
                                                                       "partially_filled"):
                    continue
                amount = float(getattr(order, "safe_amount", 0.0) or 0.0)
                filled = float(getattr(order, "safe_filled", 0.0) or 0.0)
                rate = float(getattr(order, "safe_price", 0.0) or price)
                total += max(amount - filled, 0.0) * rate
            except (AttributeError, TypeError, ValueError):
                continue
        return total

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
                       current_rate: float, current_profit: float) -> float | None:
        """Sleeve-specific position adjustment (scheduled DCA, rebalance). Lowest priority."""
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

    # ---------------------------------------------------------------- gate helpers

    def _gated_add(self, pair: str, stake: float, ps: PortfolioState, *, trade=None,
                   tag: str = "") -> float | None:
        """EVERY positive adjustment goes through here: check_entry then cap_stake."""
        if stake <= 0:
            return None
        decision = self.gate.check_entry(pair, stake, ps)
        if not decision.allowed:
            self._journal_adjust(pair, trade, False, decision.reason, decision.checks,
                                 stake, ps, action="reject")
            return None
        capped = self.gate.cap_stake(pair, stake, ps)
        capped = self._clamp_to_exchange(pair, capped)
        if capped <= 0:
            self._journal_adjust(pair, trade, False, "exchange_limits", decision.checks,
                                 stake, ps, action="reject")
            return None
        self._journal_adjust(pair, trade, True, tag or "add", decision.checks, capped, ps,
                             action="clamp" if capped < stake else "allow")
        if tag:
            self._pending_add_tag[pair] = tag
        return capped

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
                        *, action: str) -> None:
        if not self._journal_on:
            return
        failing = reason.split(":")[0]
        severity = "allow" if allowed else ("breach" if failing in _BREACH_CHECKS else "reject")
        _journal.record_gate_decision(
            self.gate_cfg.sleeve, pair, "adjust_trade_position", "adjust", allowed, reason,
            severity=severity, checks=checks, proposed_stake=stake, nav=ps.nav,
            gross_exposure=ps.gross_exposure, strategy_version=STRATEGY_VERSION,
            run_id=self.gate_cfg.run_id or None, action=action,
            trade_id=getattr(trade, "id", None),
        )

    # ---------------------------------------------------------------- callbacks

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._reload_params()
        ps = self._portfolio_state(current_time)
        actions = self.gate.loop_tick(ps)
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
        # HIGH #10: only the DAILY stop takes a timed pair lock. The monthly stop is
        # held by the gate's own monthly_locked flag plus flatten_pending(), so a
        # human resume is not fighting a ten-year freqtrade lock afterwards.
        if actions.lock_until is None:
            return
        for pair in self.gate_cfg.pairs:
            try:
                self.lock_pair(pair, until=actions.lock_until, reason=actions.flatten_reason)
            except Exception:
                pass

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime,
                            entry_tag: str | None, side: str, **kwargs) -> bool:
        ps = self._portfolio_state(current_time)
        quote = self._capture_quote(pair)
        stake = amount * rate
        d = self.gate.check_entry(pair, stake, ps)
        if d.allowed:
            self._pending_quotes[pair] = quote
        if self._journal_on:
            failing = d.reason.split(":")[0]
            severity = "allow" if d.allowed else (
                "breach" if failing in _BREACH_CHECKS else "reject")
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "confirm_trade_entry", "entry", d.allowed,
                d.reason, side=side, severity=severity, checks=d.checks,
                proposed_stake=stake, quote=quote, nav=ps.nav,
                gross_exposure=ps.gross_exposure, strategy_version=STRATEGY_VERSION,
                run_id=self.gate_cfg.run_id or None,
                action="allow" if d.allowed else "reject",
            )
        return d.allowed

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        ps = self._portfolio_state(current_time)
        quote = self._capture_quote(pair)
        self._pending_quotes[pair] = quote
        d = self.gate.check_discretionary_exit(pair, amount * rate, ps, exit_reason)
        if self._journal_on:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "confirm_trade_exit", "exit", d.allowed,
                exit_reason if d.allowed else f"{d.reason}:{exit_reason}", side="sell",
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
            return 0.0
        if max_stake:
            desired = min(desired, max_stake)
        capped = self.gate.cap_stake(pair, desired, ps)
        capped = self._clamp_to_exchange(pair, capped)
        if self._journal_on and 0 < capped < desired:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "custom_stake_amount", "entry", True,
                "clamped", side=side, checks={"clamped": True}, proposed_stake=desired,
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
        reason = self.gate.flatten_pending()
        if reason:
            return reason
        return self._custom_exit_extra(pair, trade)

    def check_entry_timeout(self, pair: str, trade, order, current_time: datetime,
                            **kwargs) -> bool:
        if self.gate._kill() or self.gate.flatten_pending():
            return True  # cancel open entry orders under KILL or an active stop
        return bool(self.mech.get("reprice_on_timeout", False))

    def check_exit_timeout(self, pair: str, trade, order, current_time: datetime,
                           **kwargs) -> bool:
        # An unfilled exit is replaced by freqtrade; repricing is the configured default
        # and a risk flatten always wants the replacement.
        if self.gate.flatten_pending():
            return True
        return bool(self.mech.get("reprice_on_timeout", False))

    # ---------------------------------------------------------------- adjust dispatcher

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit,
                              min_stake, max_stake, current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        """The single dispatcher (spec section 9). Sleeves override ``_sleeve_adjust``.

        Priority per trade per candle: flatten pending → TP ladder → DCA/pyramid →
        sleeve rebalance. Exactly one action is returned; ``None`` means do nothing.
        """
        return self._mechanics_adjust(trade, current_time, current_rate, current_profit,
                                      min_stake, max_stake)

    def _mechanics_adjust(self, trade, current_time, current_rate, current_profit,
                          min_stake, max_stake):
        now = _aware(current_time)
        if self.gate.flatten_pending():
            return None  # custom_exit owns the flatten; never add or trim beside it
        ps = self._portfolio_state(now)
        if not ps.valid:
            return None

        actions = mx.ActionSet()
        tp = self._ladder_action(trade, ps, current_rate, current_profit, min_stake)
        if tp is not None:
            actions.offer("take_profit", tp)
        add = self._add_action(trade, ps, now, current_rate, current_profit, min_stake)
        if add is not None:
            actions.offer("add", add)
        sleeve = self._sleeve_adjust(trade, ps, now, current_rate, current_profit)
        if sleeve is not None:
            actions.offer("rebalance", sleeve)

        chosen = actions.choose()
        return None if chosen is None else chosen[1]

    # -- take profit ------------------------------------------------------------

    def _ladder_action(self, trade, ps: PortfolioState, current_rate: float,
                       current_profit: float, min_stake: float | None = None) -> float | None:
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
        self._set_tdata(trade, "tp_rungs", list(decision.fired))
        if not decision.acts:
            self._journal_adjust(pair, trade, False, f"tp_skip:{decision.reason}", None,
                                 0.0, ps, action="reject")
            return None
        gate = self.gate.check_discretionary_exit(pair, decision.sell_stake, ps,
                                                 f"tp{decision.rung + 1}")
        if not gate.allowed:
            self._journal_adjust(pair, trade, False, gate.reason, gate.checks,
                                 decision.sell_stake, ps, action="reject")
            return None
        self._journal_adjust(pair, trade, True, decision.reason, gate.checks,
                             -decision.sell_stake, ps, action="partial_exit")
        return -abs(decision.sell_stake)

    # -- adds -------------------------------------------------------------------

    def _add_action(self, trade, ps: PortfolioState, now: datetime, current_rate: float,
                    current_profit: float, min_stake: float | None) -> float | None:
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
        stake = self._gated_add(pair, stake, ps, trade=trade, tag=decision.tag)
        if stake is None:
            return None
        used = dca_used if kind_key == "dca_adds" else pyr_used
        self._set_tdata(trade, kind_key, used + 1)
        self._set_tdata(trade, stamp_key, _iso(now))
        return stake

    def _clamp_add_to_target(self, pair: str, stake: float, ps: PortfolioState) -> float:
        """In target-weight sizing an add may never push past ``target_w * nav``."""
        if str(self.mech.get("sizing_mode", "target_weight")) != "target_weight":
            return stake
        target_w = self._target_weight(pair)
        if target_w <= 0:
            return 0.0
        headroom = target_w * ps.nav - ps.positions.get(pair, 0.0)
        return max(min(stake, headroom), 0.0)

    def _target_weight(self, pair: str) -> float:
        """The sleeve's current target weight for ``pair`` (0 disables target clamping)."""
        return self.gate_cfg.weight_caps.get(pair, 0.0)

    # ---------------------------------------------------------------- fills

    def order_filled(self, pair: str, trade, order, current_time: datetime, **kwargs) -> None:
        quote = self._pending_quotes.pop(pair, None)
        is_entry = order.ft_order_side == trade.entry_side
        now = _aware(current_time)
        amount = float(order.safe_filled or 0)
        price = float(order.safe_price or 0)
        fee = float(getattr(order, "fee_cost", 0.0) or 0.0)
        if self._journal_on:
            oid = _journal.record_order(
                self.gate_cfg.sleeve, pair, order.ft_order_side, order.order_type or "limit",
                amount, price, "filled",
                ft_trade_id=trade.id, ft_order_id=str(order.order_id),
                mode=self.gate_cfg.mode, run_id=self.gate_cfg.run_id or None,
            )
            _journal.record_fill(
                self.gate_cfg.sleeve, pair, order.ft_order_side, amount, price,
                order_id=oid, fee_amount=order.fee_cost, fee_currency=order.fee_currency,
                ft_order_id=str(order.order_id), quote=quote,
                mode=self.gate_cfg.mode, run_id=self.gate_cfg.run_id or None,
            )
        exit_reason = str(getattr(trade, "exit_reason", "") or "")
        self.gate.record_order_fill(now, notional=amount * price, fee_usdt=fee,
                                    risk_exit=(not is_entry) and mx.is_risk_exit(exit_reason))
        add_tag = self._pending_add_tag.pop(pair, "")
        if is_entry:
            self.gate.record_entry_fill(now)
            if add_tag == "scheduled_dca" or str(getattr(trade, "enter_tag", "") or "") == "dca":
                # Scheduled DCA is stamped on the FILL, not on submission (spec section 9),
                # so a cancelled or unfilled chunk does not consume the interval.
                self.gate.store.set(f"last_dca_fill_{pair}", _iso(now))
            if not self._tdata(trade, "first_stake", None):
                self._set_tdata(trade, "first_stake", amount * price)
            return
        self._stamp_exit(pair, trade, exit_reason, now)

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
