"""Sleeve A — the rules sleeve: 200d-MA trend regime (with hysteresis), volatility
targeting, calendar DCA top-ups. Parameters come from config/params-sleeve-a.json
(tier 1, reloaded on mtime change and clamped to earn.yaml bounds); every limit and
every mechanic comes from config, never from this file.

The only thing this class adds to ``EarnBaseStrategy`` is *what* it wants: target
weights from the rules, and a calendar DCA chunk. How that is sized, priced, gated and
clamped lives in ``earn_base``/``riskgate``/``mechanics``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from freqtrade.strategy import informative
from pandas import DataFrame

try:
    from strategies import mechanics as mx
    from strategies import sleeve_common as sc
    from strategies.earn_base import EarnBaseStrategy
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import mechanics as mx
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
        atr_period = int((self.mech.get("stoploss") or {}).get("atr", {}).get("period", 14))
        dataframe["atr"] = sc.atr(dataframe, atr_period)
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
        if self._reentry_blocked(pair, ps.now):
            return 0.0
        gap = sc.desired_stake_for_target(
            self._target_weight(pair), ps.nav, ps.positions.get(pair, 0.0))
        if entry_tag == "dca":
            return min(gap, self._scheduled_chunk(ps))
        return gap

    # ------------------------------------------------------------------ calendar DCA

    def _scheduled(self) -> dict:
        """``trading.*.scheduled_dca`` with the tier-1 params file as the override."""
        cfg = dict(self.mech.get("scheduled_dca") or {})
        for key, source in (("interval_days", "interval_days"), ("chunk_pct_nav", "chunk_pct_nav")):
            value = self._p("dca", source, default=None)
            if value is not None:
                cfg[key] = value
        return cfg

    def _scheduled_chunk(self, ps: PortfolioState) -> float:
        return float(self._scheduled().get("chunk_pct_nav", 0.05) or 0.0) * ps.nav

    def _dca_due(self, pair: str, now: datetime) -> bool:
        """Due only ``interval_days`` after the last DCA **fill** (spec section 9)."""
        raw = self.gate.store.get(f"last_dca_fill_{pair}")
        last = None
        if raw:
            try:
                last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                last = None
            else:
                last = last if last.tzinfo else last.replace(tzinfo=UTC)
        return mx.scheduled_dca_due(self._scheduled(), now=now, last_fill=last)

    def _sleeve_adjust(self, trade, ps: PortfolioState, current_time: datetime,
                       current_rate: float, current_profit: float) -> float | None:
        """Sleeve A's calendar DCA: one chunk toward the target, gated like any entry.

        The ``last_dca_fill_<pair>`` stamp is written in ``order_filled`` — on the
        FILL, not here on submission — so a cancelled or unfilled chunk does not
        consume the week's DCA.
        """
        pair = str(trade.pair)
        if self._reentry_blocked(pair, current_time):
            return None
        target_w = self._target_weight(pair)
        if target_w <= 0 or not self._dca_due(pair, current_time):
            return None
        gap = target_w * ps.nav - ps.positions.get(pair, 0.0)
        if gap <= 0 or mx.within_band(gap, ps.nav, self.gate_cfg.rebalance_band):
            return None
        chunk = min(gap, self._scheduled_chunk(ps))
        return self._gated_add(pair, chunk, ps, trade=trade, tag="scheduled_dca")
