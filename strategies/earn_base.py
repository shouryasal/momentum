"""EarnBaseStrategy: the freqtrade adapter around the pure RiskGate. TIER 2.

Both sleeves inherit this. Every enforcement callback delegates to strategies/riskgate.py;
this class only translates freqtrade's objects (wallets, trades, orders) into the pure
PortfolioState and journals what the gate decided.

Import note: inside the freqtrade container the strategy directory itself is on
sys.path (no package parent), host-side tests import the `strategies` package — hence
the dual import blocks.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from freqtrade.strategy import IStrategy
from pandas import DataFrame

try:
    from strategies import _journal
    from strategies.riskgate import (
        GateConfig, MemoryStateStore, PortfolioState, RiskGate, SqliteStateStore,
    )
except ImportError:  # in-container flat layout
    import _journal
    from riskgate import (
        GateConfig, MemoryStateStore, PortfolioState, RiskGate, SqliteStateStore,
    )

STRATEGY_VERSION = "earn-1"

# Confirm-stage rejects for sizing limits mean cap_stake was bypassed upstream — that is
# a breach worth an alert; operational refusals are routine rejects.
_BREACH_CHECKS = {"weight_cap", "gross_cap", "usdt_floor"}


class EarnBaseStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False
    process_only_new_candles = True
    position_adjustment_enable = True
    use_exit_signal = True
    startup_candle_count = 1320  # 220 days of 1d informative on a 4h base

    # Spec §6/§9: limit entries/exits, market only for stop paths. Config JSON must NOT
    # carry order_types (it would overwrite this dict wholesale).
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
        self.stoploss = -self.gate_cfg.stoploss_per_trade
        super().__init__(config)
        runmode = getattr(config.get("runmode"), "value", str(config.get("runmode", "")))
        self._is_backtest = runmode in ("backtest", "hyperopt", "plot")
        self._journal_on = not self._is_backtest
        if self._is_backtest:
            store = MemoryStateStore()
            self.gate = RiskGate(
                self.gate_cfg, store,
                flags_provider=lambda pair, now: (False, ""),
                staleness_provider=lambda now: 0.0,
                kill_provider=lambda: False,
            )
        else:
            store = SqliteStateStore(
                os.environ.get("EARN_JOURNAL_DB", "/freqtrade/journal/journal.db"),
                self.gate_cfg.sleeve,
            )
            self.gate = RiskGate(self.gate_cfg, store)
        self._pending_quotes: dict[str, tuple[str, float, float]] = {}
        self._params: dict = {}
        self._params_mtime: float = 0.0

    # ---------------------------------------------------------------- protections

    @property
    def protections(self) -> list[dict]:
        c = self.gate_cfg
        tf_h = 4
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
                "lookback_period_candles": 6,
                "trade_limit": 2,
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

    def _portfolio_state(self, now: datetime) -> PortfolioState:
        quote = self.config["stake_currency"]
        free = float(self.wallets.get_free(quote) or 0.0)
        positions: dict[str, float] = {}
        for pair in self.gate_cfg.pairs:
            base = pair.split("/")[0]
            amt = float(self.wallets.get_total(base) or 0.0)
            positions[pair] = amt * self._last_price(pair) if amt else 0.0
        nav = free + sum(positions.values())
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return PortfolioState(nav=nav, free_usdt=free, positions=positions, now=now)

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

    # ---------------------------------------------------------------- params (tier 1)

    def _reload_params(self) -> None:
        p = Path(self.gate_cfg.config_dir) / f"params-sleeve-{self.gate_cfg.sleeve}.json"
        try:
            m = p.stat().st_mtime
            if m != self._params_mtime:
                self._params = json.loads(p.read_text()).get("params", {})
                self._params_mtime = m
        except (OSError, json.JSONDecodeError):
            pass  # keep last good params

    # ---------------------------------------------------------------- sleeve hooks

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        return proposed

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        return None

    # ---------------------------------------------------------------- callbacks

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._reload_params()
        ps = self._portfolio_state(current_time)
        actions = self.gate.loop_tick(ps)
        if actions.flatten:
            if self._journal_on:
                _journal.record_gate_decision(
                    self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", False,
                    actions.flatten_reason, severity="breach",
                    nav=ps.nav, gross_exposure=sum(ps.positions.values()) / max(ps.nav, 1e-9),
                    strategy_version=STRATEGY_VERSION,
                )
            if not self._is_backtest:
                until = actions.lock_until or (current_time + timedelta(days=3650))
                for pair in self.gate_cfg.pairs:
                    try:
                        self.lock_pair(pair, until=until, reason=actions.flatten_reason)
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
                gross_exposure=sum(ps.positions.values()) / max(ps.nav, 1e-9),
                strategy_version=STRATEGY_VERSION,
            )
        return d.allowed

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        ps = self._portfolio_state(current_time)
        quote = self._capture_quote(pair)
        self._pending_quotes[pair] = quote
        d = self.gate.check_exit(pair, exit_reason, ps)
        if self._journal_on:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "confirm_trade_exit", "exit", True,
                exit_reason, side="sell", checks=d.checks, quote=quote, nav=ps.nav,
                strategy_version=STRATEGY_VERSION,
            )
        return True

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
        if self._journal_on and 0 < capped < desired:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, pair, "custom_stake_amount", "entry", True,
                "clamped", side=side, checks={"clamped": True}, proposed_stake=desired,
                nav=ps.nav, strategy_version=STRATEGY_VERSION,
            )
        return capped

    def custom_entry_price(self, pair: str, trade, current_time: datetime,
                           proposed_rate: float, entry_tag: str | None, side: str,
                           **kwargs) -> float:
        if self._is_backtest:
            return proposed_rate
        try:
            ob = self.dp.orderbook(pair, 1)
            return float(ob["bids"][0][0])  # join the bid: maker entry
        except Exception:
            return proposed_rate

    def custom_exit_price(self, pair: str, trade, current_time: datetime,
                          proposed_rate: float, current_profit: float,
                          exit_tag: str | None, **kwargs) -> float:
        if self._is_backtest:
            return proposed_rate
        try:
            ob = self.dp.orderbook(pair, 1)
            if exit_tag and exit_tag.startswith("risk_stop"):
                # Marketable limit: cross the spread so the flatten fills immediately;
                # unfilled after exit_timeout_count replacements -> emergency_exit (market).
                return float(ob["bids"][0][0]) * (1 - self.gate_cfg.cross_ticks_buffer)
            return float(ob["asks"][0][0])  # join the ask: maker exit
        except Exception:
            return proposed_rate

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
        return False

    def order_filled(self, pair: str, trade, order, current_time: datetime, **kwargs) -> None:
        quote = self._pending_quotes.pop(pair, None)
        is_entry = order.ft_order_side == trade.entry_side
        if self._journal_on:
            oid = _journal.record_order(
                self.gate_cfg.sleeve, pair, order.ft_order_side, order.order_type or "limit",
                float(order.safe_filled or 0), float(order.safe_price or 0), "filled",
                ft_trade_id=trade.id, ft_order_id=str(order.order_id),
            )
            _journal.record_fill(
                self.gate_cfg.sleeve, pair, order.ft_order_side,
                float(order.safe_filled or 0), float(order.safe_price or 0),
                order_id=oid, fee_amount=order.fee_cost, fee_currency=order.fee_currency,
                ft_order_id=str(order.order_id), quote=quote,
            )
        if is_entry:
            self.gate.record_entry_fill(
                current_time if current_time.tzinfo else current_time.replace(tzinfo=UTC))
