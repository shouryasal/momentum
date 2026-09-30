"""Sleeve B — the Claude sleeve: trades the newest valid proposal through the SAME
risk gate as Sleeve A.

Sleeve B has NO targets of its own. Everything it trades descends from a proposal, and
``_target_source`` records which of the four states produced ``self._targets``:

``none``     no valid proposal has EVER been adopted (nothing in ``proposals/``, or only
             files the loader refused, or an unapproved one in propose mode). Targets are
             zero, ``populate_entry_trend`` raises no candidate and ``_desired_stake``
             returns 0: sleeve B does not enter at all. The drift to sleeve A is NOT
             available here — drifting with no proposal ever received would be trading a
             decision nobody made. The reason is journalled (``targets:no_proposal_ever``)
             on every state change, so the Gate page shows why the sleeve is idle.
``proposal`` a valid (approved, non-stale, non-abstain) proposal was adopted this loop.
``hold``     no new usable proposal, but one was adopted less than ``drift_to_a_after_h``
             ago: the last targets stand (a fresh ``abstain`` renews the clock, and a
             stale-but-structurally-valid file on disk changes nothing — it is refused by
             the loader and the sleeve simply keeps holding).
``drift_a``  ``drift_to_a_after_h`` has passed since the last valid proposal: follow
             sleeve A's rules targets, computed from this sleeve's own indicators.

Each state carries its own ``enter_tag`` (``ENTRY_TAGS``), so the journal and the UI can
tell a proposal trade from a held one from a sleeve-A drift; the tag never claims
"proposal" unless a proposal really produced the weights.

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
    from strategies.earn_base import AdjustPlan, EarnBaseStrategy
    from strategies.riskgate import PortfolioState
except ImportError:  # in-container flat layout
    import _journal
    import mechanics as mx
    import proposal_loader as pl
    import sleeve_common as sc
    from earn_base import AdjustPlan, EarnBaseStrategy
    from riskgate import PortfolioState

# Where self._targets came from. See the module docstring.
SOURCE_NONE = "none"
SOURCE_PROPOSAL = "proposal"
SOURCE_HOLD = "hold"
SOURCE_DRIFT_A = "drift_a"

#: The ``enter_tag`` each source is allowed to put on a trade. A source with no entry
#: here (``SOURCE_NONE``) may not enter at all — sleeve B is not allowed to invent a
#: "proposal" trade when no proposal exists.
ENTRY_TAGS = {
    SOURCE_PROPOSAL: "proposal",
    SOURCE_HOLD: "proposal_hold",
    SOURCE_DRIFT_A: "drift_sleeve_a",
}


class SleeveB(EarnBaseStrategy):

    _targets: dict[str, float] = {}
    _plan: dict = {}
    #: Fail closed: until a proposal is actually adopted the sleeve has no mandate.
    _target_source: str = SOURCE_NONE
    #: Pairs the live proposal NAMED (a sparse v4 proposal names a handful of a hundred).
    #: A named zero is a decision to be flat and executes as one; a merely absent asset is
    #: wound down through the rebalance band instead — see :meth:`_named_zero`.
    _named: frozenset[str] = frozenset()

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
        tag = ENTRY_TAGS.get(self._target_source)
        if tag is None:
            # No proposal has ever been adopted: raise no candidate at all.
            dataframe["enter_long"] = 0
            return dataframe
        dataframe["enter_long"] = 1
        dataframe["enter_tag"] = tag
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
                self._named = frozenset(
                    p for p in self.gate_cfg.pairs if p.split("/")[0] in prop.targets)
                self._adopt_plan(prop)
                self.gate.store.set("sleeveb_named", ",".join(sorted(self._named)))
                self.gate.store.set("sleeveb_targets", json.dumps(targets))
                self.gate.store.set("sleeveb_targets_ts", prop.ts.isoformat())
            if prop.run_id != last_run:
                self.gate.store.set("sleeveb_run_id", prop.run_id)
                if self._journal_on:
                    _journal.record_proposal_consumption(prop.run_id, "consumed")
            if not prop.abstain:
                self._set_source(SOURCE_PROPOSAL, f"proposal:{prop.run_id}")
                return

        # No (new) tradeable proposal. Three cases, and only one of them trades sleeve
        # A's rules targets — see the module docstring.
        ts = self._last_proposal_ts()
        if ts is None:
            # Never adopted a valid proposal. Fail closed: no targets, no entries. The
            # drift clock has not started, because nothing has ever started it.
            self._no_targets("no_proposal_ever")
            return
        if (now - ts).total_seconds() <= self.gate_cfg.drift_to_a_after_h * 3600:
            raw = self.gate.store.get("sleeveb_targets")
            targets = None
            if raw:
                try:
                    targets = json.loads(raw)
                except json.JSONDecodeError:
                    targets = None
            if not isinstance(targets, dict):
                # Only abstains so far (or an unreadable store): there is nothing to
                # hold, and holding nothing means trading nothing.
                self._no_targets("no_targets_yet" if not raw else "targets_unreadable")
                return
            try:
                self._targets = {p: float(targets.get(p, 0.0) or 0.0)
                                 for p in self.gate_cfg.pairs}
            except (TypeError, ValueError):
                self._no_targets("targets_unreadable")
                return
            self._named = frozenset(
                a for a in (self.gate.store.get("sleeveb_named") or "").split(",") if a)
            self._set_source(SOURCE_HOLD, "hold_last_targets")
            return
        # drift_to_a_after_h without a valid proposal: follow sleeve A's rules targets.
        self._targets = self._rules_targets()
        self._named = frozenset(self._targets)   # the rules name every pair they size
        self._set_source(SOURCE_DRIFT_A, "drift_to_sleeve_a")

    def _no_targets(self, reason: str) -> None:
        self._targets = {pair: 0.0 for pair in self.gate_cfg.pairs}
        # NAME nothing. A zero target is only an instruction to sell when a proposal put it
        # there (see :meth:`_named_zero`); with no mandate the sleeve holds what it has and
        # buys nothing. Clearing the set here means a mandate that vanishes mid-session
        # cannot leave the previous decision's names behind to be read as a fresh sell.
        self._named = frozenset()
        self._set_source(SOURCE_NONE, reason)

    def _set_source(self, source: str, reason: str) -> None:
        """Record where the current targets came from, journalling every CHANGE.

        The loop runs every few seconds; only transitions are written, so
        ``gate_decisions`` shows "sleeve B has no proposal and is therefore not trading"
        once per state change rather than thousands of times an hour.
        """
        self._target_source = source
        stamp = f"{source}|{reason}"
        if self.gate.store.get("sleeveb_target_source") == stamp:
            return
        self.gate.store.set("sleeveb_target_source", stamp)
        tradeable = source != SOURCE_NONE
        print(f"SleeveB: targets from {source} ({reason}); "
              f"entry_tag={ENTRY_TAGS.get(source, 'none — no entries')}", file=sys.stderr)
        if self._journal_on:
            _journal.record_gate_decision(
                self.gate_cfg.sleeve, "ALL", "bot_loop_start", "loop", tradeable,
                f"targets:{reason}", severity="allow" if tradeable else "reject",
                checks={"has_mandate": tradeable, "source": source},
                run_id=self.gate_cfg.run_id or None,
                action="allow" if tradeable else "reject",
            )

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

    def _last_proposal_ts(self) -> datetime | None:
        """When the last VALID proposal was produced, or ``None`` if there never was one."""
        raw = self.gate.store.get("sleeveb_targets_ts")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    def _newest_proposal_at(self, pair: str) -> datetime | None:
        return self._last_proposal_ts()

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
        if self._target_source == SOURCE_NONE:
            return 0.0   # no proposal ever adopted: nothing authorises an entry
        if entry_tag in set(ENTRY_TAGS.values()) and entry_tag != ENTRY_TAGS[
                self._target_source]:
            # The candidate was tagged by an earlier state (the dataframe is analysed
            # once per candle). Stay flat rather than open a trade whose tag lies about
            # where its size came from; the next candle re-tags and may enter.
            return 0.0
        target_w = self._target_weight(pair)
        if target_w < self.gate_cfg.dust_weight:
            return 0.0
        if self._reentry_blocked(pair, ps.now):
            return 0.0  # stopped out recently and no newer proposal: stay flat
        gap = sc.desired_stake_for_target(target_w, ps.nav, ps.committed(pair))
        if mx.within_band(gap, ps.nav, self.gate_cfg.rebalance_band):
            return 0.0  # inside the dead-band: no churn
        return gap

    def _rebalance_band(self) -> float:
        rb = self.mech.get("rebalance") or {}
        if str(rb.get("band_source", "execution")) == "trading":
            return float(rb.get("band", self.gate_cfg.rebalance_band))
        return self.gate_cfg.rebalance_band

    def _sleeve_adjust(self, trade, ps: PortfolioState, current_time: datetime,
                       current_rate: float, current_profit: float) -> AdjustPlan | None:
        """Drift back toward the proposal's target weight, respecting the band and cadence.

        Returns a PLAN. This candidate has the LOWEST priority, so an add or a TP rung
        on the same candle beats it — and then neither the ``partial_exit`` journal row
        nor the ``last_rebalance_<pair>`` cadence stamp may be written, or the Gate page
        shows a trim that never happened and the sleeve refuses to retry it for
        ``rebalance.min_interval_hours``.
        """
        pair = str(trade.pair)
        if self.gate.kill_engaged():
            # Every size on this path comes from a proposal file another process wrote, and
            # KILL is engaged exactly when that file's provenance is in doubt. Behaviour-neutral
            # for the add branch, which check_entry already refuses on checks["kill"]; the point
            # is the SELL branches below. Before the cadence read, so a suspended trim does not
            # burn the `last_rebalance_<pair>` stamp and then refuse to retry for
            # rebalance.min_interval_hours once the human lifts KILL.
            return None
        target_w = self._target_weight(pair)
        if target_w < self.gate_cfg.dust_weight and self._named_zero(pair):
            return None  # a decided zero: full close handled by custom_exit target_zero
        if target_w < self.gate_cfg.dust_weight:
            # Merely ABSENT from a sparse proposal. Under a wide universe absence is the
            # normal state of a hundred names, so it must not mean "market-dump this into
            # whatever book exists right now" (wide-universe.md §1.5). Fall through: the
            # trim path below walks the position to zero through the rebalance band, and
            # once the residual is too small to manage :meth:`_unwind_exhausted` hands it
            # to custom_exit to finish.
            if self._unwind_exhausted(pair, ps):
                return None
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
        gap = target_w * ps.nav - ps.committed(pair)
        if mx.within_band(gap, ps.nav, self._rebalance_band()):
            return None
        stamp = current_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if gap > 0:
            if self._reentry_blocked(pair, current_time):
                return None
            plan = self._gated_add(pair, gap, ps, trade=trade, tag="rebalance")
            if plan is None:
                return None
            return plan.then(lambda: self.gate.store.set(f"last_rebalance_{pair}", stamp))
        trim = abs(gap)
        decision = self.gate.check_discretionary_exit(pair, trim, ps, "rebalance")
        if not decision.allowed:
            self._journal_adjust(pair, trade, False, decision.reason, decision.checks,
                                 -trim, ps, action="reject", is_entry=False)
            return None
        trim = self._clamp_to_exchange(pair, trim)
        if trim <= 0:
            return None
        # The gate, turnover and exchange filters all judged the MARKET value; freqtrade
        # wants the same slice expressed against the trade's cost basis.
        position_value = float(getattr(trade, "amount", 0.0) or 0.0) * float(current_rate or 0.0)
        stake = self._cost_basis_exit(trade, trim, position_value)
        if stake is None:
            return None
        plan = AdjustPlan(stake=stake)   # negative stake: trim the position toward target
        plan.then(lambda: self._journal_adjust(pair, trade, True, "rebalance_trim",
                                               decision.checks, -trim, ps,
                                               action="partial_exit", is_entry=False))
        return plan.then(lambda: self.gate.store.set(f"last_rebalance_{pair}", stamp))

    def _named_zero(self, pair: str) -> bool:
        """Is being flat in this pair a DECISION rather than an omission?

        Yes when the PROPOSAL named the asset — at zero, or at a weight the exposure scale
        took to zero. That is a decision, and a decision may sell.

        No when there is no mandate at all. ``SOURCE_NONE`` used to mean "flatten", and that
        was wrong twice over. Factually: the absence of a proposal is the absence of a
        decision, not a decision to sell — and ``SOURCE_NONE`` is reached by plumbing, not by
        judgement. The store is namespaced ``run:<run_id>:`` while freqtrade's trades table is
        not, so a minted run id, an unreadable runtime overlay, a memory-store fallback or one
        loop with an empty ``proposals/`` directory made the mandate vanish while the
        positions remained. On 2026-09-23 that sold the whole sleeve at market fifteen minutes
        after it bought, 57 seconds before it adopted a valid proposal (−27.56 USDT, 15.01 of
        it fees). Measured: `crisis-policy.md` §0 puts "sell everything on a trigger" at
        −5.23% CAGR against +30.53% for holding and not buying.

        So no mandate now means **hold what you have and buy nothing** — entries are already
        refused for ``SOURCE_NONE`` at :meth:`_sleeve_adjust`. A real flatten still has two
        routes that both require a decision: a proposal that names the asset at zero, and the
        operator's own kill switch or console flatten.
        """
        return pair in self._named

    def _unwind_exhausted(self, pair: str, ps: PortfolioState) -> bool:
        """Has an orderly wind-down shrunk this position past the point of slicing it?

        The threshold is the **rebalance band**, not ``min_position_pct_nav``, because the
        band is what stops the trim: once the whole remaining position is inside the
        dead-band, every further slice is judged "no churn" and the position would sit
        there forever. At that point the remainder is handed to ``custom_exit``, and the
        decision is recorded in the gate's store so it survives a restart and reads in the
        journal as the end of a wind-down rather than as a stop.
        """
        nav = max(float(ps.nav), 1e-9)
        floor = max(self._rebalance_band(), self.gate_cfg.min_position_pct_nav)
        if ps.positions.get(pair, 0.0) / nav > floor:
            return False
        self.gate.store.set(f"unwind_done_{pair}", "1")
        return True

    def _custom_exit_extra(self, pair: str, trade) -> str | None:
        """``target_zero`` when the proposal has decided this name to zero, else ``None``.

        Suspended under KILL, and this is the most important of the three seats: ``target_zero``
        is a RISK exit reason, so it is waved through ``check_discretionary_exit`` with no checks
        at all — it is the most permissive sell in the system. Its authority is a proposal file
        another process wrote, which is the one thing KILL says not to trust. A stop, a flatten
        or a human force-exit still closes this position; the sleeve simply stops deciding to.
        """
        if self.gate.kill_engaged():
            return None
        if self._target_weight(pair) >= self.gate_cfg.dust_weight:
            self.gate.store.set(f"unwind_done_{pair}", "")
            return None
        if self._named_zero(pair) or self.gate.store.get(f"unwind_done_{pair}") == "1":
            return "target_zero"
        return None   # orderly wind-down in progress; _sleeve_adjust is trimming it
