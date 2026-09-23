"""Sleeve B — the Claude sleeve: trades the newest valid proposal through the SAME
risk gate as Sleeve A. Holds last valid targets when no proposal; after
``drift_to_a_after_h`` without a valid one, drifts to Sleeve A's rules targets computed
from its own indicators.

In propose mode (``runtime-b.json: require_approval``) a proposal is only adopted once
an HMAC-signed, unexpired approval for its run id exists in the approvals directory —
verified here, inside the container, with stdlib only. An unapproved proposal is held,
not traded: fail closed.
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
    from strategies import mechanics as mx
    from strategies import proposal_loader as pl
    from strategies import sleeve_common as sc
    from strategies.earn_base import EarnBaseStrategy
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import _journal
    import mechanics as mx
    import proposal_loader as pl
    import sleeve_common as sc
    from earn_base import EarnBaseStrategy
    from riskgate import PortfolioState


class SleeveB(EarnBaseStrategy):

    _targets: dict[str, float] = {}
    _plan: dict = {}

    # Same 1d indicators as SleeveA: needed for the drift to rules targets.
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
        atr_period = int((self.mech.get("stoploss") or {}).get("atr", {}).get("period", 14))
        dataframe["atr"] = sc.atr(dataframe, atr_period)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Entries are decided by the current targets + gate, not by candle history:
        # the signal is a standing candidate; custom_stake_amount returns 0 when the
        # target, the rebalance band or the re-entry cooldown says no.
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

    def _approved(self, prop, now: datetime) -> bool:
        """Propose mode: the in-container HMAC check on ``proposals/approved/``."""
        if not self.gate_cfg.require_approval:
            return True
        ok, reason = pl.approval_for(self.gate_cfg.approvals_dir, prop.run_id, now,
                                     proposal_path=prop.path)
        if not ok:
            print(f"SleeveB: proposal {prop.run_id} not approved ({reason})", file=sys.stderr)
            if self._journal_on:
                _journal.record_proposal_consumption(prop.run_id, "rejected",
                                                     f"approval:{reason}")
        return ok

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
        if prop is not None and not self._approved(prop, now):
            prop = None
        if prop is not None:
            last_run = self.gate.store.get("sleeveb_run_id")
            if prop.abstain:
                # Hold current targets; a fresh abstain still resets the drift clock.
                self.gate.store.set("sleeveb_targets_ts", prop.ts.isoformat())
            else:
                weights = pl.effective_targets(prop, assets)
                targets = {p: weights[p.split("/")[0]] for p in self.gate_cfg.pairs}
                self._targets = targets
                self._adopt_plan(prop)
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

    def _adopt_plan(self, prop) -> None:
        """Clamp the optional ``plan`` block into ``trading.plan_bounds`` before use."""
        plan, changed = pl.clamp_plan(prop.plan, self.gate_cfg.plan_bounds)
        self._plan = plan
        self.gate.store.set("sleeveb_plan", json.dumps(plan))
        if changed and self._journal_on:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", True,
                "plan_clamped:" + ",".join(sorted(changed)), severity="reject",
                checks={"plan_clamped": False}, run_id=self.gate_cfg.run_id or None,
                action="clamp",
            )

    def _newest_proposal_at(self, pair: str) -> datetime | None:
        raw = self.gate.store.get("sleeveb_targets_ts")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

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

    def _target_weight(self, pair: str) -> float:
        return float(self._targets.get(pair, 0.0))

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        target_w = self._target_weight(pair)
        if target_w < self.gate_cfg.dust_weight:
            return 0.0
        if self._reentry_blocked(pair, ps.now):
            return 0.0  # stopped out recently and no newer proposal: stay flat
        gap = sc.desired_stake_for_target(target_w, ps.nav, ps.positions.get(pair, 0.0))
        if mx.within_band(gap, ps.nav, self.gate_cfg.rebalance_band):
            return 0.0  # inside the dead-band: no churn
        return gap

    def _rebalance_band(self) -> float:
        rb = self.mech.get("rebalance") or {}
        if str(rb.get("band_source", "execution")) == "trading":
            return float(rb.get("band", self.gate_cfg.rebalance_band))
        return self.gate_cfg.rebalance_band

    def _sleeve_adjust(self, trade, ps: PortfolioState, current_time: datetime,
                       current_rate: float, current_profit: float) -> float | None:
        """Drift back toward the proposal's target weight, respecting the band and cadence."""
        pair = str(trade.pair)
        target_w = self._target_weight(pair)
        if target_w < self.gate_cfg.dust_weight:
            return None  # full close handled by custom_exit target_zero
        last = self.gate.store.get(f"last_rebalance_{pair}")
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00")) if last else None
        except (TypeError, ValueError):
            last_dt = None
        if last_dt is not None and last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        if not mx.rebalance_allowed(
                now=current_time, last_rebalance=last_dt,
                min_interval_hours=float((self.mech.get("rebalance") or {}).get(
                    "min_interval_hours", 0))):
            return None
        gap = target_w * ps.nav - ps.positions.get(pair, 0.0)
        if mx.within_band(gap, ps.nav, self._rebalance_band()):
            return None
        stamp = current_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if gap > 0:
            if self._reentry_blocked(pair, current_time):
                return None
            stake = self._gated_add(pair, gap, ps, trade=trade, tag="rebalance")
            if stake is None:
                return None
            self.gate.store.set(f"last_rebalance_{pair}", stamp)
            return stake
        trim = abs(gap)
        decision = self.gate.check_discretionary_exit(pair, trim, ps, "rebalance")
        if not decision.allowed:
            self._journal_adjust(pair, trade, False, decision.reason, decision.checks,
                                 -trim, ps, action="reject")
            return None
        trim = self._clamp_to_exchange(pair, trim)
        if trim <= 0:
            return None
        self._journal_adjust(pair, trade, True, "rebalance_trim", decision.checks, -trim, ps,
                             action="partial_exit")
        self.gate.store.set(f"last_rebalance_{pair}", stamp)
        return -trim  # negative stake: trim the position toward target

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        if self._target_weight(pair) < self.gate_cfg.dust_weight:
            return "target_zero"
        return None
