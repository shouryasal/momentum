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


class SleeveA(EarnBaseStrategy):

    #: Book targets are cross-sectional and cached per candle — see :meth:`_book_targets`.
    _targets_cache: dict[str, float] | None = None
    _targets_stamp: tuple | None = None

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
        self._instrument_signals(dataframe, metadata)
        return dataframe

    def _instrument_signals(self, dataframe: DataFrame, metadata: dict) -> None:
        """Per-month count of the entry signals this sleeve PRODUCED (``EARN_INSTRUMENT_CSV``).

        The denominator for "why so few trades": every candle in an up regime carries
        ``enter_long=1``, so the signal count is huge and the trade count is not — the
        difference is what the sizing and the gate did, which the other taps record.
        """
        if not instrument_on():
            return
        try:
            sig = dataframe.loc[dataframe.get("enter_long").eq(1), ["date", "enter_tag"]]
            months = sig["date"].dt.strftime("%Y-%m")
            for (month, tag), n in sig.groupby([months, "enter_tag"]).size().items():
                instrument("signal", str(metadata.get("pair", "")), month, str(tag),
                           float(n))
        except Exception:  # noqa: BLE001 — a diagnostic may never break the loop
            pass

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["regime_1d"].fillna(0) == 0, "exit_long"] = 1
        return dataframe

    # ------------------------------------------------------------------ sizing

    def _pair_state(self, pair: str) -> tuple[bool, float, object] | None:
        """``(regime_up, annualised realised vol, candle stamp)`` from the pair's 1d frame."""
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            last = df.iloc[-1]
            rvol = float(last["rvol_1d"])
        except Exception:
            return None
        if rvol != rvol or rvol <= 0:   # NaN or nonsense vol: no risk budget, no position
            return None
        return bool(last["regime_1d"]), rvol, last.get("date")

    def _satellites(self, order: list[str]) -> list[str]:
        """This week's satellites, from the snapshot's scores with rotation hysteresis.

        Incumbency is read from the gate's own state store, so a restart does not reset
        the hysteresis band and start the book churning. Absent a snapshot there are no
        scores, no satellites, and the sleeve is exactly the BTC/ETH sleeve it was.
        """
        cfg = self.gate_cfg
        scores = {a: s for a, s in cfg.universe.scores.items()
                  if cfg.universe.is_satellite(a) and a in order}
        if not scores:
            return []
        raw = self.gate.store.get("satellites") or ""
        incumbents = [a for a in raw.split(",") if a]
        chosen = sc.select_satellites(
            scores, incumbents,
            max_n=int(cfg.max_satellite_positions),
            hysteresis_ranks=int(self._p("satellites", "hysteresis_ranks", default=3)),
        )
        if chosen != incumbents:
            self.gate.store.set("satellites", ",".join(chosen))
        return chosen

    def _book_targets(self) -> dict[str, float]:
        """Target weight per PAIR for the whole book — core plus rotated satellites.

        Built once per candle for the WHOLE book rather than once per pair, for two
        reasons. Vol targeting is now a book-level control: sizing each name against its
        own vol lets eight names that are each inside the target add up to a book that is
        not (wide-universe.md §2.1, §3.2). And satellite selection is cross-sectional —
        "the best four" is not a question any single pair can answer. The result is cached
        on the last candle stamp because every entry callback asks for it and a whitelist
        of 30 pairs would otherwise read 900 dataframes per loop.

        ``sleeve_common.core_satellite_targets`` is the one implementation of the maths,
        shared with the vectorised strategy-lab harness so a sweep and the live sleeve
        cannot disagree about what the rules say.
        """
        cfg = self.gate_cfg
        quote = str(self.config.get("stake_currency", "USDT"))
        core = {a: float(w) for a, w in (self._p("base_weights", default={}) or {}).items()}
        # One reference pair is the clock. Reading every pair's frame just to decide
        # whether the cache is stale would reintroduce the O(pairs^2) the cache exists to
        # avoid: BTC's candle closes when everyone's does.
        clock = next((p for p in cfg.pairs if p.split("/")[0] == "BTC"),
                     cfg.pairs[0] if cfg.pairs else "")
        clock_state = self._pair_state(clock) if clock else None
        stamp = (clock, str(clock_state[2]) if clock_state else "")
        if clock_state is not None and self._targets_stamp == stamp \
                and self._targets_cache is not None:
            return self._targets_cache
        states: dict[str, tuple[bool, float, object]] = {}
        for pair in cfg.pairs:
            st = clock_state if pair == clock else self._pair_state(pair)
            if st is not None:
                states[pair.split("/")[0]] = st
        # The whole book's regime gate is BTC's own 200d MA: below it the satellite sleeve
        # goes to cash. That single rule is the difference between the equal-weight wide
        # book's −96.6% drawdown and the gated core's −50.9%.
        risk_on = bool(states.get("BTC", (False, 0.0, None))[0])
        eligible = [a for a in states if a not in core and cfg.universe.is_satellite(a)]
        satellites = [a for a in self._satellites(sorted(eligible))
                      if states.get(a, (False,))[0]]
        targets = sc.core_satellite_targets(
            core_weights={a: w for a, w in core.items() if a in states},
            core_regime_up={a: states[a][0] for a in core if a in states},
            satellites=satellites,
            vols={a: v for a, (_, v, _s) in states.items()},
            caps={a: cfg.cap_for(f"{a}/{quote}") for a in states},
            vol_target_annual=self._p("vol", "target_annual", default=0.30),
            satellite_gross=float(cfg.max_satellite_gross),
            risk_on=risk_on,
        )
        out = {f"{a}/{quote}": w for a, w in targets.items()}
        self._targets_stamp, self._targets_cache = stamp, out
        return out

    def _target_weight(self, pair: str) -> float:
        try:
            return float(self._book_targets().get(pair, 0.0))
        except Exception:
            return 0.0

    def _desired_stake(self, pair: str, ps: PortfolioState, proposed: float,
                       entry_tag: str | None) -> float:
        if self._reentry_blocked(pair, ps.now):
            instrument("want_none", pair, ps.now, f"reentry_cooldown:{entry_tag or ''}")
            return 0.0
        target = self._target_weight(pair)
        gap = sc.desired_stake_for_target(target, ps.nav, ps.positions.get(pair, 0.0))
        if entry_tag == "dca":
            gap = min(gap, self._scheduled_chunk(ps))
        if gap <= 0:
            instrument("want_none", pair, ps.now,
                       ("target_zero" if target <= 0 else "no_gap")
                       + f":{entry_tag or ''}")
        else:
            instrument("want", pair, ps.now, entry_tag or "", gap)
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
                       current_rate: float, current_profit: float) -> AdjustPlan | None:
        """Sleeve A's calendar DCA: one chunk toward the target, gated like any entry.

        The ``last_dca_fill_<pair>`` stamp is written in ``order_filled`` — on the
        FILL, not here on submission — so a cancelled or unfilled chunk does not
        consume the week's DCA. The ``scheduled_dca`` tag rides out on the plan and
        comes back as ``order.ft_order_tag``, so only THIS chunk's own fill stamps it.
        """
        pair = str(trade.pair)
        if self._reentry_blocked(pair, current_time):
            instrument("dca_none", pair, current_time, "reentry_cooldown")
            return None
        target_w = self._target_weight(pair)
        if target_w <= 0:
            instrument("dca_none", pair, current_time, "target_zero")
            return None
        if not self._dca_due(pair, current_time):
            instrument("dca_none", pair, current_time, "not_due")
            return None
        gap = target_w * ps.nav - ps.positions.get(pair, 0.0)
        if gap <= 0:
            instrument("dca_none", pair, current_time, "no_gap")
            return None
        if mx.within_band(gap, ps.nav, self.gate_cfg.rebalance_band):
            instrument("dca_none", pair, current_time, "within_band", gap)
            return None
        chunk = min(gap, self._scheduled_chunk(ps))
        return self._gated_add(pair, chunk, ps, trade=trade, tag="scheduled_dca")
