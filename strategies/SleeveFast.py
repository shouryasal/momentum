"""SleeveFast — the short-horizon TEST strategy. TIER 2.

A sibling of ``SleeveA``, never a change to it. It exists for one job: to make every code
path the shipped system owns — entry sizing, the gate's checks, limit pricing, fills, the
take-profit ladder, ROI, the trailing stop, the re-entry cooldown, the rebalance band, the
journal and the local watcher — fire inside a ten-hour observation window, where the
shipped 4h/200-day-MA sleeve would see two or three candles and do nothing.

It is selected by a PROFILE (``config/profiles/fast-test.yaml``, chosen with
``profiles.active``), so nothing about ``SleeveA``, ``SleeveB`` or the shipped defaults
moves when it runs, and switching back is one key.

**This is a plumbing test, not an edge.** The rule below — a fast/slow EMA trend with a
short breakout confirmation and a realised-volatility band — is a conventional intraday
trend entry, and it is *not* one of the measured findings in
``docs/design/wide-universe.md``. What that research did measure is used where it applies:
cross-sectional 90-day momentum has a rank IC of −0.067 (t = −7.4) and is deliberately not
a selector here either; the sleeve does not rank names at all, it treats every whitelisted
pair symmetrically and lets the gate's tier caps decide how much of each it may hold.

What it deliberately does NOT do:

* it proposes target weights and never sizes past them — every stake still goes through
  ``RiskGate.cap_stake`` then ``check_entry``, like any other sleeve's;
* it reads no limit of its own. ``trading.defaults.fast`` carries signal settings only;
  every cap, stop, cooldown and daily counter is the gate's, unchanged;
* it never trims toward its own target. A standing target plus a trim would fight the
  take-profit ladder for the same position on the same candle, and the ladder is the point
  of this profile — so ``_sleeve_adjust`` tops up and nothing else, and exits are the
  ladder, ROI, the stop and the trend-loss signal.

Two overrides earn their place, and both are about keeping a *shipped* guarantee intact on
a 1h timeframe rather than about going faster:

``startup_candle_count``
    ``EarnBaseStrategy`` derives it from ``bounds['sleeve_a.trend.ma_days'].max + 20`` days
    — 320 days, which is 7,680 candles at 1h for indicators whose longest lookback is 21.
    This class honours the configured ``trading.startup_candles`` instead.

``_daily_returns``
    The gate's ``max_beta_to_btc`` and ``max_avg_pairwise_corr`` were measured on 60 DAILY
    observations. The base implementation reads the strategy frame, which at 1h would hand
    the gate 60 *hourly* returns under the same name — a weaker check wearing the label of
    the measured one. This resamples the 1h frame to daily closes, so both caps keep the
    window they were calibrated on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pandas import DataFrame

try:
    from strategies import mechanics as mx
    from strategies import sleeve_common as sc
    from strategies.earn_base import (
        AdjustPlan,
        EarnBaseStrategy,
        instrument,
        instrument_on,
    )
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import mechanics as mx
    import sleeve_common as sc
    from earn_base import AdjustPlan, EarnBaseStrategy, instrument, instrument_on
    from riskgate import PortfolioState

#: Every entry this sleeve raises carries this tag, so a journal row, a TCA fill and the
#: Gate page can all tell a fast-profile trade from a rules or proposal trade at a glance.
ENTRY_TAG = "fast_breakout"

#: Floor on ``startup_candle_count`` whatever the config says: the EMA, the breakout window
#: and the ATR all need to be warm before the first candle is allowed to trade, and a
#: profile that set ``startup_candles: 5`` would otherwise enter on NaNs.
MIN_STARTUP_SLACK = 20

#: Gate-store key holding the UTC timestamp of this sleeve's last NEW-trade entry fill.
#: Sleeve-wide and persisted through the gate's own store, so the spacing survives a
#: container restart rather than resetting the clock every time the bot is bounced.
LAST_ENTRY_KEY = "fast_last_entry"


class SleeveFast(EarnBaseStrategy):
    """Fast EMA trend + short breakout, inside the unchanged risk gate."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._fast: dict = dict(self.mech.get("fast") or {})
        self.startup_candle_count = self._startup_needed()

    # ------------------------------------------------------------------ settings

    def _f(self, key: str, default):
        value = self._fast.get(key, default)
        return default if value is None else value

    def _startup_needed(self) -> int:
        """The configured startup, floored at what the indicators actually need."""
        needed = MIN_STARTUP_SLACK + max(
            int(self._f("ema_slow", 21)) * 3,
            int(self._f("breakout_lookback", 6)) + 1,
            int(self._f("atr_period", 14)) + 1,
        )
        return max(int(self.gate_cfg.startup_candles or 0), needed)

    def _rebalance_band(self) -> float:
        rb = self.mech.get("rebalance") or {}
        if str(rb.get("band_source", "execution")) == "trading":
            return float(rb.get("band", self.gate_cfg.rebalance_band))
        return self.gate_cfg.rebalance_band

    # ------------------------------------------------------------------ indicators

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast = int(self._f("ema_fast", 9))
        slow = int(self._f("ema_slow", 21))
        lookback = int(self._f("breakout_lookback", 6))
        close = dataframe["close"]
        dataframe["ema_fast"] = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
        dataframe["ema_slow"] = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
        dataframe["atr"] = sc.atr(dataframe, int(self._f("atr_period", 14)))
        dataframe["atr_pct"] = dataframe["atr"] / close.where(close > 0)
        # `shift(1)` so the window is PRIOR highs: comparing a close against a maximum that
        # includes its own candle makes every new high a "breakout" and is lookahead by
        # another name.
        dataframe["breakout_high"] = (
            dataframe["high"].rolling(lookback, min_periods=lookback).max().shift(1)
            if lookback > 0 else float("-inf")
        )
        dataframe["trend_up"] = dataframe["ema_fast"] > dataframe["ema_slow"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr_pct = dataframe["atr_pct"]
        candidate = (
            dataframe["trend_up"].fillna(False)
            & (dataframe["close"] > dataframe["breakout_high"])
            & (atr_pct >= float(self._f("min_atr_pct", 0.0)))
            & (atr_pct <= float(self._f("max_atr_pct", 1.0)))
        )
        dataframe["enter_long"] = 0
        dataframe.loc[candidate, "enter_long"] = 1
        dataframe.loc[candidate, "enter_tag"] = ENTRY_TAG
        self._instrument_signals(dataframe, metadata)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        if self._f("exit_on_trend_loss", True):
            dataframe.loc[~dataframe["trend_up"].fillna(True), "exit_long"] = 1
        return dataframe

    def _instrument_signals(self, dataframe: DataFrame, metadata: dict) -> None:
        """Per-day count of the entry signals this sleeve PRODUCED (``EARN_INSTRUMENT_CSV``).

        The whole cadence question is "does the signal supply exceed what the gate will
        allow", and that is the only tap that answers it: ``risk.max_trades_per_day`` caps
        the trades at four, so a trade count alone cannot distinguish "the rule fired six
        times and the gate allowed four" from "the rule fired four times".
        """
        if not instrument_on():
            return
        try:
            sig = dataframe.loc[dataframe["enter_long"].eq(1), ["date"]]
            days = sig["date"].dt.strftime("%Y-%m-%d")
            for day, n in sig.groupby(days).size().items():
                instrument("signal", str(metadata.get("pair", "")), day, ENTRY_TAG,
                           float(n))
        except Exception:  # noqa: BLE001 — a diagnostic may never break the loop
            pass

    # ------------------------------------------------------------------ risk inputs

    def _trend_up(self, pair: str) -> bool:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            return bool(df["trend_up"].iloc[-1])
        except Exception:  # noqa: BLE001 — unknown trend is not an up trend
            return False

    def _regime_up(self, pair: str) -> bool:
        """This sleeve's regime IS its EMA trend; there is no 1d frame to read."""
        return self._trend_up(pair)

    def _daily_returns(self, pair: str) -> list[float] | None:
        """Daily returns resampled from the intraday frame — see the module docstring."""
        window = int(self._f("risk_window_days", 60))
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            daily = (df.set_index("date")["close"].resample("1D").last()
                     .dropna().tail(window + 1))
            if len(daily) < 3:
                return None
            return [float(x) for x in daily.pct_change().dropna()]
        except Exception:  # noqa: BLE001 — a risk check may never break the loop
            return None

    # ------------------------------------------------------------------ sizing

    def _target_weight(self, pair: str) -> float:
        """Flat target per pair, clamped to the human ceiling for that asset's tier.

        No ranking and no cross-sectional score: this profile is about event count, and
        ranking 31 names by anything would be asserting an edge it has not measured. The
        cap is the gate's, so a satellite still tops out at 5% of NAV and a name the
        snapshot has no tier for still resolves to zero and is refused.
        """
        cap = float(self.gate_cfg.cap_for(pair))
        if cap <= 0:
            return 0.0
        return min(float(self._f("target_pct_nav", 0.05)), cap)

    def _entry_spaced(self, now: datetime) -> bool:
        """Has enough time passed since this sleeve's last NEW trade? (cadence, not a limit)

        The measured signal supply on the 1h profile is 40-220 candidates a Gulf day across
        the 31-pair whitelist against a ``risk.max_trades_per_day`` of 4, so the budget is
        spent within the first candles of the day and the remaining twenty hours are silent
        — which is precisely what makes a ten-hour observation window a coin flip. Spacing
        spends the same allowance evenly. It can only ever *delay* an entry, never authorise
        one: the gate still decides.
        """
        spacing = float(self._f("min_entry_spacing_min", 0) or 0)
        if spacing <= 0:
            return True
        raw = self.gate.store.get(LAST_ENTRY_KEY)
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (now - last).total_seconds() >= spacing * 60.0

    def order_filled(self, pair: str, trade, order, current_time: datetime,
                     **kwargs) -> None:
        """Stamp the spacing clock on a NEW trade's first entry fill, then journal as usual.

        On the FILL and not on submission, and only for the first entry of a trade: a
        cancelled or unfilled order must not consume the spacing, and a rebalance top-up is
        not a new trade.
        """
        super().order_filled(pair, trade, order, current_time, **kwargs)
        try:
            if order.ft_order_side == trade.entry_side and self._entries_used(trade) <= 1:
                stamp = current_time if current_time.tzinfo else current_time.replace(
                    tzinfo=UTC)
                self.gate.store.set(
                    LAST_ENTRY_KEY,
                    stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001 — freqtrade swallows this callback; never raise
            pass

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        if not self._entry_spaced(ps.now):
            instrument("want_none", pair, ps.now, f"entry_spacing:{entry_tag or ''}")
            return 0.0
        if self._reentry_blocked(pair, ps.now):
            instrument("want_none", pair, ps.now, f"reentry_cooldown:{entry_tag or ''}")
            return 0.0
        target = self._target_weight(pair)
        gap = sc.desired_stake_for_target(target, ps.nav, ps.committed(pair))
        if gap <= 0:
            instrument("want_none", pair, ps.now,
                       ("target_zero" if target <= 0 else "no_gap") + f":{entry_tag or ''}")
            return 0.0
        if mx.within_band(gap, ps.nav, self._rebalance_band()):
            instrument("want_none", pair, ps.now, f"within_band:{entry_tag or ''}", gap)
            return 0.0
        instrument("want", pair, ps.now, entry_tag or "", gap)
        return gap

    # ------------------------------------------------------------------ exits / adds

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        """Leave a name the universe has dropped: its cap is zero, so hold nothing.

        ``target_zero`` is a risk exit (``mechanics.RISK_EXIT_REASONS``), so it is never
        blocked by a churn or turnover check and it prices as a marketable limit. The
        weekly resolver is what moves a name to ``exit_only`` or out of the tiers; this is
        the sleeve noticing.
        """
        if self._target_weight(pair) <= 0:
            return "target_zero"
        return None

    def _sleeve_adjust(self, trade, ps: PortfolioState, current_time: datetime,
                       current_rate: float, current_profit: float) -> AdjustPlan | None:
        """Top up toward the target when the position has drifted outside the band.

        Top-ups ONLY, and only while the trend still holds. A trim toward a standing target
        would compete with the take-profit ladder for the same position on the same candle,
        and ``mechanics.ACTION_PRIORITY`` would hand it to the ladder anyway — so the
        sleeve never offers one, and a position leaves through the ladder, ROI, the stop or
        the trend-loss exit signal.

        Returns a PLAN: this candidate has the lowest priority, so the cadence stamp may
        only be written once it has actually won.
        """
        pair = str(trade.pair)
        if self._tdata(trade, "tp_rungs", []) or []:
            # A rung has BOOKED part of this position. Topping it back up to the target is
            # buying back what was just sold: two orders, two spreads, two fees and no
            # change in exposure. Once the ladder has started on a trade, the trade is on
            # its way out and the sleeve leaves it alone.
            instrument("rebal_none", pair, current_time, "tp_fired")
            return None
        if not self._trend_up(pair):
            instrument("rebal_none", pair, current_time, "trend_down")
            return None
        if self._reentry_blocked(pair, current_time):
            instrument("rebal_none", pair, current_time, "reentry_cooldown")
            return None
        target_w = self._target_weight(pair)
        if target_w <= 0:
            instrument("rebal_none", pair, current_time, "target_zero")
            return None
        last = self.gate.store.get(f"last_rebalance_{pair}")
        try:
            last_dt = (datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                       if last else None)
        except (TypeError, ValueError):
            last_dt = None
        if last_dt is not None and last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        if not mx.rebalance_allowed(
                now=current_time, last_rebalance=last_dt,
                min_interval_hours=float((self.mech.get("rebalance") or {}).get(
                    "min_interval_hours", 0))):
            instrument("rebal_none", pair, current_time, "cadence")
            return None
        gap = target_w * ps.nav - ps.committed(pair)
        if gap <= 0 or mx.within_band(gap, ps.nav, self._rebalance_band()):
            instrument("rebal_none", pair, current_time, "within_band", gap)
            return None
        plan = self._gated_add(pair, gap, ps, trade=trade, tag="rebalance")
        if plan is None:
            return None
        stamp = current_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return plan.then(lambda: self.gate.store.set(f"last_rebalance_{pair}", stamp))
