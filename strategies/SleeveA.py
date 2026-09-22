"""Sleeve A — the rules sleeve: 200d-MA trend regime (with hysteresis), volatility
targeting, weekly DCA top-ups. Parameters come from config/params-sleeve-a.json
(tier 1, reloaded on mtime change); bounds live in earn.yaml (tier 2).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from freqtrade.strategy import informative
from pandas import DataFrame

try:
    from strategies import sleeve_common as sc
    from strategies.earn_base import EarnBaseStrategy
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import sleeve_common as sc
    from earn_base import EarnBaseStrategy
    from riskgate import PortfolioState


class SleeveA(EarnBaseStrategy):

    def _p(self, *keys, default=None):
        d = self._params or {}
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._reload_params()
        return sc.add_1d_indicators(
            dataframe,
            ma_days=self._p("trend", "ma_days", default=200),
            hysteresis_pct=self._p("trend", "hysteresis_pct", default=0.02),
            vol_lookback_days=self._p("vol", "lookback_days", default=20),
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = sc.atr(dataframe, 14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = dataframe["regime_1d"].fillna(0)
        fresh_cross = (regime > 0) & (regime.shift(1) == 0)
        dataframe.loc[regime > 0, "enter_long"] = 1
        dataframe.loc[regime > 0, "enter_tag"] = "dca"
        dataframe.loc[fresh_cross, "enter_tag"] = "trend"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["regime_1d"].fillna(0) == 0, "exit_long"] = 1
        return dataframe

    # ------------------------------------------------------------------ sizing

    def _target_weight(self, pair: str) -> float:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            last = df.iloc[-1]
            if not last["regime_1d"]:
                return 0.0
            asset = pair.split("/")[0]
            return sc.vol_scaled_weight(
                self._p("base_weights", asset, default=0.0),
                self._p("vol", "target_annual", default=0.30),
                float(last["rvol_1d"]),
                self.gate_cfg.weight_caps.get(pair, 0.0),
            )
        except Exception:
            return 0.0

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        gap = sc.desired_stake_for_target(
            self._target_weight(pair), ps.nav, ps.positions.get(pair, 0.0))
        if entry_tag == "dca":
            chunk = self._p("dca", "chunk_pct_nav", default=0.05) * ps.nav
            return min(gap, chunk)
        return gap

    # ------------------------------------------------------------------ DCA top-ups

    def _dca_due(self, pair: str, now: datetime) -> bool:
        raw = self.gate.store.get(f"last_dca_fill_{pair}")
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        return now - last >= timedelta(days=self._p("dca", "interval_days", default=7))

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit,
                              min_stake, max_stake, current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        pair = trade.pair
        ps = self._portfolio_state(current_time)
        target_w = self._target_weight(pair)
        if target_w <= 0 or not self._dca_due(pair, current_time):
            return None
        gap = target_w * ps.nav - ps.positions.get(pair, 0.0)
        if gap / max(ps.nav, 1e-9) <= self.gate_cfg.rebalance_band:
            return None
        chunk = min(gap, self._p("dca", "chunk_pct_nav", default=0.05) * ps.nav)
        d = self.gate.check_entry(pair, chunk, ps)
        if not d.allowed:
            return None
        stake = self.gate.cap_stake(pair, chunk, ps)
        if stake <= 0:
            return None
        self.gate.store.set(f"last_dca_fill_{pair}",
                            current_time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return stake
