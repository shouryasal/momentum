"""Sleeve B — the Claude sleeve: trades the newest valid proposal through the SAME
risk gate as Sleeve A. Holds last valid targets when no proposal; after 48h without
a valid one, drifts to Sleeve A's rules targets computed from its own indicators.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.strategy import informative
from pandas import DataFrame

try:
    from strategies import _journal
    from strategies import proposal_loader as pl
    from strategies import sleeve_common as sc
    from strategies.earn_base import EarnBaseStrategy
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import _journal
    import proposal_loader as pl
    import sleeve_common as sc
    from earn_base import EarnBaseStrategy
    from riskgate import PortfolioState


class SleeveB(EarnBaseStrategy):

    _targets: dict[str, float] = {}

    # Same 1d indicators as SleeveA: needed for the 48h drift to rules targets.
    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        params = self._sleeve_a_params()
        return sc.add_1d_indicators(
            dataframe,
            ma_days=params.get("trend", {}).get("ma_days", 200),
            hysteresis_pct=params.get("trend", {}).get("hysteresis_pct", 0.02),
            vol_lookback_days=params.get("vol", {}).get("lookback_days", 20),
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Entries are decided by the current targets + gate, not by candle history:
        # the signal is a standing candidate; custom_stake_amount returns 0 when the
        # target or the rebalance band says no.
        dataframe["enter_long"] = 1
        dataframe["enter_tag"] = "proposal"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    # ------------------------------------------------------------------ proposal loop

    def _sleeve_a_params(self) -> dict:
        try:
            p = Path(self.gate_cfg.config_dir) / "params-sleeve-a.json"
            return json.loads(p.read_text()).get("params", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        super().bot_loop_start(current_time, **kwargs)
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=UTC)
        assets = [p.split("/")[0] for p in self.gate_cfg.pairs]

        def on_reject(path: str, reason: str) -> None:
            print(f"SleeveB: skipping proposal {path}: {reason}", file=sys.stderr)

        prop = pl.load_newest_valid(
            self.gate_cfg.proposals_dir, assets, self.gate_cfg.proposal_max_age_h,
            self.gate_cfg.proposal_sum_tolerance, now, on_reject=on_reject,
        )
        if prop is not None:
            last_run = self.gate.store.get("sleeveb_run_id")
            if prop.abstain:
                # Hold current targets; a fresh abstain still resets the drift clock.
                self.gate.store.set("sleeveb_targets_ts", prop.ts.isoformat())
            else:
                weights = pl.effective_targets(prop, assets)
                targets = {p: weights[p.split("/")[0]] for p in self.gate_cfg.pairs}
                self._targets = targets
                self.gate.store.set("sleeveb_targets", json.dumps(targets))
                self.gate.store.set("sleeveb_targets_ts", prop.ts.isoformat())
            if prop.run_id != last_run:
                self.gate.store.set("sleeveb_run_id", prop.run_id)
                if self._journal_on:
                    _journal.record_proposal_consumption(prop.run_id, "consumed")
            if not prop.abstain:
                return

        # No (new) tradeable proposal: keep persisted targets; after drift_to_a_after_h
        # without a valid proposal, follow sleeve A's rules targets.
        raw = self.gate.store.get("sleeveb_targets")
        ts_raw = self.gate.store.get("sleeveb_targets_ts")
        stale = True
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                stale = (now - ts).total_seconds() > self.gate_cfg.drift_to_a_after_h * 3600
            except ValueError:
                stale = True
        if raw and not stale:
            try:
                self._targets = json.loads(raw)
                return
            except json.JSONDecodeError:
                pass
        self._targets = self._rules_targets()

    def _rules_targets(self) -> dict[str, float]:
        params = self._sleeve_a_params()
        latest: dict[str, sc.AssetIndicators] = {}
        for pair in self.gate_cfg.pairs:
            try:
                df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                last = df.iloc[-1]
                latest[pair.split("/")[0]] = sc.AssetIndicators(
                    close=float(last["close_1d"]), sma_ma=float(last["sma_ma_1d"]),
                    regime_up=bool(last["regime_1d"]), rvol_annual=float(last["rvol_1d"]),
                )
            except Exception:
                latest[pair.split("/")[0]] = sc.AssetIndicators(0.0, 0.0, False, 0.0)
        weights = sc.compute_rules_targets(
            latest,
            base_weights=params.get("base_weights", {}),
            weight_caps={p.split("/")[0]: c for p, c in self.gate_cfg.weight_caps.items()},
            vol_target_annual=params.get("vol", {}).get("target_annual", 0.30),
        )
        return {p: weights.get(p.split("/")[0], 0.0) for p in self.gate_cfg.pairs}

    # ------------------------------------------------------------------ sizing

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        target_w = self._targets.get(pair, 0.0)
        if target_w < self.gate_cfg.dust_weight:
            return 0.0
        gap = sc.desired_stake_for_target(target_w, ps.nav, ps.positions.get(pair, 0.0))
        if gap / max(ps.nav, 1e-9) <= self.gate_cfg.rebalance_band:
            return 0.0  # inside the dead-band: no churn
        return gap

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit,
                              min_stake, max_stake, current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        pair = trade.pair
        ps = self._portfolio_state(current_time)
        target_w = self._targets.get(pair, 0.0)
        if target_w < self.gate_cfg.dust_weight:
            return None  # full close handled by custom_exit target_zero
        gap = target_w * ps.nav - ps.positions.get(pair, 0.0)
        if abs(gap) / max(ps.nav, 1e-9) <= self.gate_cfg.rebalance_band:
            return None
        if gap > 0:
            d = self.gate.check_entry(pair, gap, ps)
            if not d.allowed:
                return None
            stake = self.gate.cap_stake(pair, gap, ps)
            return stake if stake > 0 else None
        return -abs(gap)  # negative stake: trim the position toward target

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        if self._targets.get(pair, 0.0) < self.gate_cfg.dust_weight:
            return "target_zero"
        return None
